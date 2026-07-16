"""APScheduler glue — Scheduler engine for the Cron feature.

Single ``AsyncIOScheduler`` instance + ``MemoryJobStore`` (sidecar in
:mod:`cron_store` is the canonical source of truth; the in-memory store is
just APScheduler's working set, rebuilt from the sidecar on every process
start). All firing routes through :func:`_fire_job` which delegates to
:mod:`cron_runner` for action execution.

Constraints (see ``app.py`` startup banner):
- uvicorn must run with ``--workers 1`` — multiple processes would each
  load and fire every job N times.
- ``misfire_grace_time=60`` + ``coalesce=True`` + ``max_instances=1`` per
  job: skips backlog avalanche on restart, drops slots missed by >60s,
  blocks parallel re-fire of the same job.
"""
from __future__ import annotations
import asyncio
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import cron_store as store
from .public_url import public_link, public_base_url

DEFAULT_TZ: str = "Europe/Warsaw"
_DEFAULT_MISFIRE_GRACE_S: int = 60
_ORPHAN_CANCEL_AFTER_S: float = 5 * 60.0
# Periodically sweep placeholder pollution in runs.jsonl. Without this the
# disk file accumulates 2 records per fire (started placeholder + final)
# until the next service restart, which can be days/weeks.
_PERIODIC_RECONCILE_INTERVAL_S: int = 10 * 60
_PERIODIC_RECONCILE_JOB_ID: str = "_internal_periodic_reconcile"

_INTERVAL_RE: re.Pattern[str] = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")

_scheduler: AsyncIOScheduler | None = None


def _warn(msg: str) -> None:
    print(f"[scheduler] {msg}", file=sys.stderr)


def is_running() -> bool:
    return _scheduler is not None and _scheduler.running


# ── trigger parsing ──────────────────────────────────────────────


def _parse_tz(tz: str | None) -> ZoneInfo:
    name = tz.strip() if isinstance(tz, str) and tz.strip() else DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ValueError(f"unknown timezone: {name!r}: {exc}") from exc


def build_trigger(trigger: dict) -> Any:
    """Convert sidecar trigger dict → APScheduler trigger object.

    Raises :class:`ValueError` on any malformed input — caller (route layer)
    maps that to HTTP 400 with the message body.
    """
    if not isinstance(trigger, dict):
        raise ValueError("trigger must be an object")
    ttype = trigger.get("type")
    spec = trigger.get("spec")
    tz = _parse_tz(trigger.get("tz"))
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("trigger.spec is required")
    spec = spec.strip()
    if ttype == "cron":
        try:
            return CronTrigger.from_crontab(spec, timezone=tz)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid cron expression {spec!r}: {exc}") from exc
    if ttype == "interval":
        seconds = _parse_interval_spec(spec)
        return IntervalTrigger(seconds=seconds, timezone=tz)
    if ttype == "date":
        run_date = _parse_iso_datetime(spec, tz)
        return DateTrigger(run_date=run_date)
    raise ValueError(f"unknown trigger type: {ttype!r}")


def _parse_interval_spec(spec: str) -> int:
    match = _INTERVAL_RE.match(spec)
    if not match:
        raise ValueError(f"interval spec must be <N>[smhd] (got {spec!r})")
    n = int(match.group(1))
    unit = match.group(2)
    if n <= 0:
        raise ValueError("interval must be positive")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return n * multipliers[unit]


def _parse_iso_datetime(spec: str, tz: ZoneInfo) -> datetime:
    candidate = spec.replace("Z", "+00:00") if spec.endswith("Z") else spec
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"date trigger spec must be ISO 8601 (got {spec!r}): {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def compute_next_fires(trigger: dict, n: int = 5) -> list[str]:
    """Return up to ``n`` upcoming fire times as ISO 8601 strings.

    Uses the trigger's own ``get_next_fire_time(prev, now)`` so we don't have
    to register a real APScheduler job just for the preview endpoint. Each
    iteration advances ``now`` past the prior fire — APScheduler's contract
    is "next fire ≥ now AND > previous_fire_time", so without bumping ``now``
    a CronTrigger keeps returning the same slot forever.
    """
    from datetime import timedelta as _td

    apsched_trigger = build_trigger(trigger)
    out: list[str] = []
    now = datetime.now(tz=_parse_tz(trigger.get("tz")))
    previous: datetime | None = None
    for _ in range(max(0, int(n))):
        try:
            nxt = apsched_trigger.get_next_fire_time(previous, now)
        except Exception as exc:
            _warn(f"compute_next_fires failed: {exc}")
            break
        if nxt is None:
            break
        out.append(nxt.isoformat())
        previous = nxt
        now = nxt + _td(microseconds=1)
    return out


# ── lifecycle ────────────────────────────────────────────────────


async def start() -> None:
    """Boot the scheduler + register every enabled job from the sidecar.

    Idempotent: a second call is a no-op. Failures registering individual
    jobs are logged but never abort the rest of the boot sequence.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    store.bootstrap()
    pruned = 0
    try:
        pruned = store.prune_runs()
    except Exception as exc:
        _warn(f"prune_runs failed at startup: {exc}")
    cancelled = 0
    try:
        cancelled = reconcile_orphans()
    except Exception as exc:
        _warn(f"reconcile_orphans failed: {exc}")

    _scheduler = AsyncIOScheduler(timezone=_parse_tz(DEFAULT_TZ))
    _scheduler.start()
    # Internal recurring job: collapse placeholder/final pairs in runs.jsonl
    # without waiting for the next service restart. Underscore-prefixed id
    # so the user-facing sidecar (cron/jobs.json) never sees it; it lives
    # only in APScheduler's MemoryJobStore.
    try:
        _scheduler.add_job(
            reconcile_orphans,
            IntervalTrigger(seconds=_PERIODIC_RECONCILE_INTERVAL_S),
            id=_PERIODIC_RECONCILE_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=None,
        )
    except Exception as exc:
        _warn(f"failed to register periodic reconcile: {exc}")
    jobs = await store.list_jobs()
    registered = 0
    for job in jobs.values():
        try:
            await register_job(job)
            registered += 1
        except Exception as exc:
            _warn(f"register_job({job.get('id')!r}) failed: {exc}")
    _warn(
        f"started with {registered}/{len(jobs)} jobs "
        f"(pruned {pruned} runs, cancelled {cancelled} orphan runs); "
        "uvicorn must run --workers 1"
    )


async def shutdown() -> None:
    """Stop the scheduler cleanly. Safe to call when never started."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception as exc:
        _warn(f"shutdown failed: {exc}")
    _scheduler = None


# ── orphan reconciliation ───────────────────────────────────────


def reconcile_orphans() -> int:
    """Mark stale ``started_but_unfinished`` runs as cancelled.

    A process restart kills any in-flight subprocess; without this sweep
    those runs would forever look "in progress" in the runs view. We treat
    any run with no ``finished_at`` and ``started_at`` older than 5 min as
    cancelled — short enough to never confuse a healthy run, long enough
    to give a slow-starting fire room to record completion.

    Returns count of records rewritten.
    """
    if not store.RUNS_PATH.exists():
        return 0
    # This runs in an APScheduler worker thread (AsyncIOScheduler dispatches a
    # sync job func via run_in_executor), so its read-snapshot → rewrite must
    # hold the shared runs-file lock to stay mutually exclusive with the
    # on-loop append_run / prune_runs — otherwise an append landing between our
    # snapshot and our os.replace would be silently dropped.
    with store._RUNS_FILE_LOCK:
        records: list[dict] = list(store._iter_runs())
        if not records:
            return 0
        real_outcomes_by_run: dict[str, list[dict]] = {}
        for rec in records:
            rid = rec.get("run_id")
            if not rid or rec.get("finished_at") is None:
                continue
            if rec.get("error") == "orchestrator restarted":
                continue
            real_outcomes_by_run.setdefault(rid, []).append(rec)
        finalised_run_ids = set(real_outcomes_by_run.keys())
        cutoff = time.time() - _ORPHAN_CANCEL_AFTER_S
        rewritten = 0
        out: list[dict] = []
        for rec in records:
            if (
                rec.get("finished_at") is None
                and float(rec.get("started_at") or 0.0) > 0.0
                and float(rec.get("started_at") or 0.0) < cutoff
            ):
                if rec.get("run_id") in finalised_run_ids:
                    # Final record already exists for this run — the placeholder
                    # is harmless garbage. Dropping it (instead of patching to
                    # cancelled) keeps dedup from picking the patched record's
                    # later finished_at and masking the real ok/failed outcome.
                    rewritten += 1
                    continue
                patched = {
                    **rec,
                    "finished_at": time.time(),
                    "duration_ms": int((time.time() - float(rec["started_at"])) * 1000),
                    "status": "cancelled",
                    "error": "orchestrator restarted",
                }
                out.append(patched)
                rewritten += 1
            elif (
                rec.get("error") == "orchestrator restarted"
                and rec.get("status") == "cancelled"
                and rec.get("run_id") in finalised_run_ids
            ):
                # Retroactive cleanup for the pre-fix bug: a previous reconcile
                # pass already patched the placeholder to cancelled while the
                # real ok/failed sibling was already present. Drop the patched
                # cancelled so the UI shows the real outcome.
                rewritten += 1
            else:
                out.append(rec)
        if rewritten > 0:
            store._rewrite_runs(out)
        return rewritten


# ── job registration ────────────────────────────────────────────


def _concurrency_to_max_instances(concurrency: str | None) -> int:
    if concurrency == "allow":
        return 5
    return 1


def _replace_existing_for_concurrency(concurrency: str | None) -> bool:
    return concurrency == "replace"


async def register_job(job: dict) -> None:
    """Add (or replace) the job in the live scheduler.

    Skips jobs with ``enabled=False`` (still kept in sidecar; resume() picks
    them back up). Validates the trigger eagerly so a malformed sidecar
    entry surfaces in the startup log instead of at first fire.
    """
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job.id is required")
    if not job.get("enabled", True):
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass
        return
    trigger = build_trigger(job.get("trigger") or {})
    concurrency = job.get("concurrency", "skip")
    _scheduler.add_job(
        _fire_job,
        trigger=trigger,
        id=job_id,
        name=job.get("name") or job_id,
        args=[job_id, False],
        replace_existing=True,
        misfire_grace_time=_DEFAULT_MISFIRE_GRACE_S,
        coalesce=True,
        max_instances=_concurrency_to_max_instances(concurrency),
    )
    nxt = _next_fire_iso(job_id)
    if nxt is not None:
        try:
            await store.patch_job(job_id, {"next_run_at": nxt})
        except Exception as exc:
            _warn(f"patch_job(next_run_at) for {job_id!r} failed: {exc}")
    _ = _replace_existing_for_concurrency  # reserved hook for future "replace" semantics


async def unregister_job(job_id: str) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass


async def pause_job(job_id: str) -> None:
    """Pause the job in the scheduler + flip sidecar ``enabled=false``.

    Tolerant of an already-missing scheduler entry (one-shot already fired,
    job was removed by ``unregister_job``, or the scheduler restarted
    without re-registering yet) — the sidecar update is the user-visible
    side effect. Without this fall-through, the route would 400 even
    though the pause semantically succeeded.
    """
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    try:
        _scheduler.pause_job(job_id)
    except Exception as exc:
        # JobLookupError (or anything else from APScheduler) → log + continue.
        # Sidecar is the source of truth for "is the job paused"; flipping
        # enabled=False is what callers actually care about.
        _warn(f"pause_job: scheduler.pause_job raised for {job_id!r}: {exc}")
    try:
        await store.patch_job(job_id, {"enabled": False, "next_run_at": None})
    except KeyError:
        raise ValueError(f"job not found: {job_id}")


async def resume_job(job_id: str) -> None:
    """Resume the job in the scheduler + flip sidecar ``enabled=true``.

    If the scheduler has no entry (e.g. a one-shot was disabled but its
    fire spec is in the past — re-registering now would raise), fall back
    to flipping the sidecar only and warn. UI shows the warning via the
    next list poll (next_run_at stays None for invalid triggers).
    """
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    try:
        _scheduler.resume_job(job_id)
    except Exception as exc:
        _warn(f"resume_job: scheduler.resume_job raised for {job_id!r}: {exc}")
    nxt = _next_fire_iso(job_id)
    try:
        await store.patch_job(job_id, {"enabled": True, "next_run_at": nxt})
    except KeyError:
        raise ValueError(f"job not found: {job_id}")


async def reschedule_job(job_id: str, trigger: dict) -> None:
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    apsched_trigger = build_trigger(trigger)
    try:
        _scheduler.reschedule_job(job_id, trigger=apsched_trigger)
    except Exception as exc:
        raise ValueError(f"cannot reschedule {job_id!r}: {exc}") from exc
    nxt = _next_fire_iso(job_id)
    if nxt is not None:
        try:
            await store.patch_job(job_id, {"next_run_at": nxt})
        except KeyError:
            pass


async def trigger_now(job_id: str) -> str:
    """Manually fire ``job_id`` once. Returns the ``run_id`` of that fire.

    Schedules the fire on the asyncio loop (does NOT await completion) so
    HTTP callers get a fast 200 with the run id and can poll runs.jsonl
    for status. ``manual=True`` so the run record is tagged.
    """
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    job = await store.get_job(job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    run_id = str(uuid.uuid4())
    asyncio.create_task(_fire_job(job_id, manual=True, run_id_override=run_id))
    return run_id


# ── firing ──────────────────────────────────────────────────────


def _next_fire_iso(job_id: str) -> str | None:
    if _scheduler is None:
        return None
    try:
        ap_job = _scheduler.get_job(job_id)
    except Exception:
        return None
    if ap_job is None or ap_job.next_run_time is None:
        return None
    return ap_job.next_run_time.isoformat()


_NOTIFY_PRIORITY_BY_STATUS: dict[str, int] = {
    "failed": 4,
    "ok": 3,
    "skipped": 2,
}


def _format_notify_message(run: dict) -> str:
    """Compose a human-readable push body from a run record."""
    duration_ms = run.get("duration_ms")
    duration_s = (
        f"{int(duration_ms) / 1000:.1f}s" if isinstance(duration_ms, (int, float))
        else "—"
    )
    exit_code = run.get("exit_code")
    lines: list[str] = [f"status: {run.get('status') or 'unknown'} · {duration_s}"]
    if exit_code is not None:
        lines.append(f"exit_code: {exit_code}")
    err = run.get("error")
    if isinstance(err, str) and err.strip():
        lines.append(f"error: {err.strip()[:200]}")
    stderr_tail = run.get("stderr_tail")
    if isinstance(stderr_tail, str) and stderr_tail.strip():
        tail = stderr_tail.strip().splitlines()[-3:]
        lines.append("stderr:")
        lines.extend(tail)
    output = run.get("output_preview")
    if isinstance(output, str) and output.strip():
        tail = output.strip().splitlines()[-3:]
        lines.append("output:")
        lines.extend(tail)
    body = "\n".join(lines)
    return body[:1500]


_DEFAULT_NOTIFY_ON = ("failed",)


async def _maybe_notify_run(job: dict, run: dict) -> None:
    """Publish a push notification for a terminal run.

    This is the SINGLE source of truth for "cron run → mobile push". Both
    failure-only legacy jobs (no ``notify`` field) and explicitly-configured
    jobs flow through here:

    - Job with no ``notify`` field → defaults to ``on=["failed"]``. Mirrors
      the prior unconditional ``cron_alerts._send_mobile_push`` behaviour
      without doubling up when the user also configures ``notify``.
    - Job with ``notify={on:[...]}`` → matches ``run.status`` against the
      allowlist (``"all"`` matches everything).
    - Job with ``notify={on:[]}`` → silenced (explicit opt-out).

    Soft-imports the notify module so a missing primitive keeps the
    scheduler running. Never raises — caller wraps anyway.
    """
    cfg = job.get("notify") if isinstance(job, dict) else None
    has_explicit_cfg = isinstance(cfg, dict)
    if has_explicit_cfg:
        on_raw = cfg.get("on")
        # Empty list = explicit "silence everything" (user opted out).
        on = list(on_raw) if isinstance(on_raw, list) else []
    else:
        # No notify field at all = legacy default: ping on failure.
        cfg = {}
        on = list(_DEFAULT_NOTIFY_ON)
    status = run.get("status")
    if not isinstance(status, str):
        return
    fire = ("all" in on) or (status in on)
    if not fire:
        return

    try:
        from . import notify as notify_mod  # type: ignore[attr-defined]
    except Exception as exc:
        _warn(f"_maybe_notify_run: notify module unavailable: {exc}")
        return

    notify_fn = getattr(notify_mod, "notify", None)
    if not callable(notify_fn):
        _warn("_maybe_notify_run: notify.notify is not callable")
        return

    name = job.get("name") or job.get("id") or "(unknown)"
    job_id = job.get("id") or "(unknown)"
    title = f"Cron · {name} · {status}"
    message = _format_notify_message(run)

    topic_override = cfg.get("topic")
    topic = (
        topic_override.strip() if isinstance(topic_override, str) and topic_override.strip()
        else "cron"
    )
    priority_cfg = cfg.get("priority")
    priority = (
        priority_cfg if isinstance(priority_cfg, int) and not isinstance(priority_cfg, bool)
        else _NOTIFY_PRIORITY_BY_STATUS.get(status, 3)
    )
    tags_for_status = {
        "failed": ["x", "rotating_light"],
        "ok": ["white_check_mark"],
        "skipped": ["fast_forward"],
    }.get(status, [])
    click = public_link(f"/scheduler/{job_id}")

    # On a failed run we surface a second inline-keyboard button that deep-links
    # straight into the diagnose flow: scheduler-detail-runs.jsx watches for
    # `#diagnose=<run_id>` on mount, finds the run, and fires the same
    # `handleDiagnose` path the manual "🤖 Diagnose with agent" button uses.
    actions: list[dict] | None = None
    run_id = run.get("run_id") if isinstance(run, dict) else None
    if status == "failed" and isinstance(run_id, str) and run_id and public_base_url():
        actions = [{
            "action": "view",
            "label": "🤖 Diagnose",
            "url": public_link(f"/scheduler/{job_id}#diagnose={run_id}"),
        }]

    try:
        await notify_fn(
            topic=topic,
            message=message,
            title=title,
            priority=priority,
            tags=tags_for_status,
            click=click,
            actions=actions,
        )
    except Exception as exc:
        _warn(f"_maybe_notify_run: notify call failed for {job_id!r}: {exc}")


async def _fire_job(
    job_id: str,
    manual: bool = False,
    run_id_override: str | None = None,
) -> None:
    """APScheduler-callback: read fresh job from store, run action, persist run.

    Defensive: every error path is caught here so a broken job never kills
    the scheduler thread. End-condition gates run BEFORE the action so a
    job that has hit its ``max_runs`` ceiling cannot fire again even if
    APScheduler still has it queued.
    """
    from . import cron_runner as runner_mod  # local import: avoid circular
    from . import cron_alerts as alerts_mod

    job = await store.get_job(job_id)
    if job is None:
        _warn(f"_fire_job: job {job_id!r} missing from store; skipping")
        await unregister_job(job_id)
        return

    trigger_label = "manual" if manual else "scheduled"
    run_id = run_id_override or str(uuid.uuid4())
    started_at = time.time()

    if not manual and not job.get("enabled", True):
        skipped_run = _record_skipped(job_id, run_id, started_at, trigger_label, "job disabled")
        try:
            await _maybe_notify_run(job, skipped_run)
        except Exception as exc:
            _warn(f"_maybe_notify_run for {job_id!r} (skipped/disabled) crashed: {exc}")
        return
    if not manual and _end_condition_reached(job):
        skipped_run = _record_skipped(job_id, run_id, started_at, trigger_label, "end_condition reached")
        try:
            await _maybe_notify_run(job, skipped_run)
        except Exception as exc:
            _warn(f"_maybe_notify_run for {job_id!r} (skipped/end-cond) crashed: {exc}")
        try:
            await store.patch_job(job_id, {"enabled": False})
            await unregister_job(job_id)
        except Exception as exc:
            _warn(f"end-condition disable failed for {job_id!r}: {exc}")
        return

    started_run = {
        "run_id": run_id,
        "job_id": job_id,
        "started_at": started_at,
        "finished_at": None,
        "duration_ms": None,
        "status": "running",
        "trigger": trigger_label,
        "session_id": None,
        "exit_code": None,
        "stderr_tail": "",
        "error": None,
    }
    try:
        store.append_run(started_run)
    except Exception as exc:
        _warn(f"append_run(start) failed for {job_id!r}: {exc}")

    try:
        result = await runner_mod.run_action(job, run_id, manual=manual)
    except Exception as exc:
        _warn(f"_fire_job: action raised for {job_id!r}: {exc}")
        result = {
            "status": "failed",
            "exit_code": None,
            "stderr_tail": "",
            "output": "",
            "session_id": None,
            "error": f"unhandled exception: {exc}",
            "started_at": started_at,
            "finished_at": time.time(),
            "duration_ms": int((time.time() - started_at) * 1000),
        }

    final = {
        **started_run,
        "started_at": result.get("started_at") or started_at,
        "finished_at": result.get("finished_at") or time.time(),
        "duration_ms": result.get("duration_ms"),
        "status": result.get("status") or "failed",
        "session_id": result.get("session_id"),
        "exit_code": result.get("exit_code"),
        "stderr_tail": result.get("stderr_tail") or "",
        "error": result.get("error"),
        "output_preview": (result.get("output") or "")[:2000],
    }
    if final["duration_ms"] is None:
        final["duration_ms"] = int((float(final["finished_at"]) - float(final["started_at"])) * 1000)
    try:
        store.append_run(final)
    except Exception as exc:
        _warn(f"append_run(final) failed for {job_id!r}: {exc}")

    await _update_job_after_fire(job_id, final)

    if final["status"] == "failed":
        try:
            await alerts_mod.report_failure(job, final)
        except Exception as exc:
            _warn(f"report_failure for {job_id!r} crashed: {exc}")

    try:
        await _maybe_notify_run(job, final)
    except Exception as exc:
        _warn(f"_maybe_notify_run for {job_id!r} crashed: {exc}")

    try:
        # prune_runs holds store._RUNS_FILE_LOCK across its snapshot → rewrite,
        # which serializes it against append_run (this loop) and the
        # worker-thread reconcile_orphans, so no concurrent append is dropped.
        # Kept on the loop: it's a background post-fire step, and append_run on
        # the same loop is already exclusive with it — the lock only matters
        # versus the worker-thread reconcile.
        store.prune_runs()
    except Exception as exc:
        _warn(f"prune_runs after fire failed: {exc}")


def _end_condition_reached(job: dict) -> bool:
    cond = job.get("end_condition") or {}
    max_runs = cond.get("max_runs")
    if isinstance(max_runs, int) and max_runs > 0:
        if int(job.get("run_count") or 0) >= max_runs:
            return True
    until_raw = cond.get("until")
    if isinstance(until_raw, str) and until_raw.strip():
        try:
            until_dt = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
        if datetime.now(tz=timezone.utc) >= until_dt.astimezone(timezone.utc):
            return True
    return False


def _record_skipped(
    job_id: str,
    run_id: str,
    started_at: float,
    trigger_label: str,
    reason: str,
) -> dict:
    """Append a skipped-run record and return it.

    Returning the dict lets the caller forward it to ``_maybe_notify_run``
    so a job with ``notify.on=["skipped"]`` actually receives the push
    (the early-skip paths in ``_fire_job`` exit before the standard
    ``_maybe_notify_run`` call site below the run, so the skipped record
    has to be hand-delivered).
    """
    rec = {
        "run_id": run_id,
        "job_id": job_id,
        "started_at": started_at,
        "finished_at": started_at,
        "duration_ms": 0,
        "status": "skipped",
        "trigger": trigger_label,
        "session_id": None,
        "exit_code": None,
        "stderr_tail": "",
        "error": reason,
    }
    try:
        store.append_run(rec)
    except Exception as exc:
        _warn(f"append_run(skipped) failed: {exc}")
    return rec


async def _update_job_after_fire(job_id: str, run: dict) -> None:
    patch: dict[str, Any] = {
        "last_run_at": float(run.get("finished_at") or time.time()),
        "last_run_status": run.get("status") or "failed",
        "next_run_at": _next_fire_iso(job_id),
    }
    job = await store.get_job(job_id)
    if job is not None:
        patch["run_count"] = int(job.get("run_count") or 0) + 1
        if run.get("status") == "failed":
            patch["failure_count"] = int(job.get("failure_count") or 0) + 1
        # One-shot DateTrigger jobs disable themselves once they've fired:
        # APScheduler removes the job from its in-memory store after a date
        # fire (next_run_at goes None), but the sidecar still says
        # enabled=True. On orchestrator restart we'd otherwise try to
        # register a DateTrigger pointing at a past timestamp — APScheduler
        # raises and the job sticks around as a zombie. Flip enabled=False
        # explicitly so the next startup leaves it alone.
        trigger = job.get("trigger") or {}
        if trigger.get("type") == "date":
            patch["enabled"] = False
    try:
        await store.patch_job(job_id, patch)
    except KeyError:
        pass
    except Exception as exc:
        _warn(f"_update_job_after_fire failed for {job_id!r}: {exc}")
    if job is not None and (job.get("trigger") or {}).get("type") == "date":
        try:
            await unregister_job(job_id)
        except Exception as exc:
            _warn(f"unregister one-shot {job_id!r} failed: {exc}")
