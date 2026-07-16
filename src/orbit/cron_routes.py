"""HTTP routes for the Scheduler (cron jobs registry).

Mounted by :func:`orbit.library.register_routes` BEFORE the
catch-all ``PATCH /api/library/{kind}/{name:path}`` so any future routes
under ``/api/cron/...`` aren't shadowed (FastAPI uses first-match).

Routes are thin shells over four sibling modules owned by Agent A:

* :mod:`cron_store`     — JSON sidecar I/O (jobs.json, runs.jsonl)
* :mod:`cron_scheduler` — APScheduler glue (register/pause/resume/trigger)
* :mod:`cron_runner`    — fire execution (llm/shell)
* :mod:`cron_alerts`    — failure watchdog (push + alerts session)

If any of those modules fail to import (e.g. Agent A's branch hasn't
landed yet) we log a warning and skip registration entirely so the rest
of the dashboard keeps booting. This module is best-effort glue.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import Body, FastAPI, HTTPException

_logger = logging.getLogger(__name__)

# ── soft imports ──────────────────────────────────────────────────
# Agent A is implementing cron_store / cron_scheduler in parallel. If
# they're not on disk yet, we register no routes and the rest of the
# app keeps working. The contracts below are the source of truth shared
# between Agents A/B/C.

try:
    from . import cron_store  # type: ignore[attr-defined]
    from . import cron_scheduler  # type: ignore[attr-defined]
    _MODULES_OK = True
except Exception as e:  # pragma: no cover — defensive
    _logger.warning(
        "cron_routes: dependent modules not available, routes disabled: %s", e,
    )
    cron_store = None  # type: ignore[assignment]
    cron_scheduler = None  # type: ignore[assignment]
    _MODULES_OK = False


# ── helpers ───────────────────────────────────────────────────────


_VALID_TRIGGER_TYPES = ("cron", "interval", "date")
_VALID_ACTION_MODES = ("llm", "shell")
_VALID_DEST_MODES = ("fresh", "rolling", "existing", "none", "telegram")
_VALID_CONCURRENCY = ("skip", "allow", "replace")
_VALID_NOTIFY_ON = ("ok", "failed", "skipped", "all")
_NOTIFY_TOPIC_MAX_LEN = 64
_DEFAULT_TZ = "Europe/Warsaw"
_MAX_NAME_LEN = 128
_MAX_COMMAND_LEN = 4096
# Optional ``action.model`` override for cron fires. Aliases are forwarded
# to claude-cli's --model flag (e.g. ``opus`` / ``sonnet`` / ``haiku``);
# full model IDs (``claude-haiku-4-5``, …) are also accepted via the regex
# below. None/empty falls back to the CLI default.
_MAX_MODEL_LEN = 64
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _http_for(exc: Exception) -> HTTPException:
    """Map domain errors to HTTP. Mirrors :func:`library._http_for`."""
    if isinstance(exc, ValueError):
        return HTTPException(400, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, detail=str(exc))
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return HTTPException(404, detail=str(exc))
    return HTTPException(500, detail=str(exc))


def _validate_job_id(job_id: str) -> str:
    """Run id through ``cron_store.safe_job_id`` → 400 on bad id."""
    try:
        return cron_store.safe_job_id(job_id)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e


def _require_str(
    payload: dict, key: str, *, max_len: int | None = None, allow_empty: bool = False,
) -> str:
    """Extract required string field; HTTPException(400) on bad input."""
    value = payload.get(key)
    if not isinstance(value, str):
        raise HTTPException(400, detail=f"{key} must be a string")
    stripped = value.strip()
    if not allow_empty and not stripped:
        raise HTTPException(400, detail=f"{key} must be a non-empty string")
    if max_len is not None and len(stripped) > max_len:
        raise HTTPException(
            400, detail=f"{key} exceeds max length ({max_len} chars)",
        )
    return stripped


def _optional_str(payload: dict, key: str, *, max_len: int | None = None) -> str | None:
    """Extract optional string field; None if missing/empty."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, detail=f"{key} must be a string")
    stripped = value.strip()
    if not stripped:
        return None
    if max_len is not None and len(stripped) > max_len:
        raise HTTPException(
            400, detail=f"{key} exceeds max length ({max_len} chars)",
        )
    return stripped


def _validate_tz(tz: str) -> str:
    """Validate IANA timezone via zoneinfo. 400 on unknown."""
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as e:
        raise HTTPException(400, detail=f"unknown timezone: {tz!r}") from e
    except Exception as e:
        raise HTTPException(400, detail=f"invalid timezone: {e}") from e
    return tz


def _validate_trigger(trigger: Any) -> dict:
    """Validate ``trigger`` block shape; 400 on any defect."""
    if not isinstance(trigger, dict):
        raise HTTPException(400, detail="trigger must be an object")
    ttype = trigger.get("type")
    if ttype not in _VALID_TRIGGER_TYPES:
        raise HTTPException(
            400,
            detail=f"trigger.type must be one of {list(_VALID_TRIGGER_TYPES)}",
        )
    spec = trigger.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise HTTPException(400, detail="trigger.spec must be a non-empty string")
    tz = trigger.get("tz") or _DEFAULT_TZ
    if not isinstance(tz, str) or not tz.strip():
        raise HTTPException(400, detail="trigger.tz must be a string")
    _validate_tz(tz)
    return {"type": ttype, "spec": spec.strip(), "tz": tz}


def _validate_end_condition(ec: Any) -> dict:
    """Validate optional ``end_condition`` block."""
    if ec is None:
        return {"max_runs": None, "until": None}
    if not isinstance(ec, dict):
        raise HTTPException(400, detail="end_condition must be an object")
    max_runs = ec.get("max_runs")
    if max_runs is not None:
        if not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs < 1:
            raise HTTPException(
                400, detail="end_condition.max_runs must be a positive integer",
            )
    until = ec.get("until")
    if until is not None:
        if not isinstance(until, str) or not until.strip():
            raise HTTPException(
                400, detail="end_condition.until must be an ISO datetime string",
            )
        try:
            from datetime import datetime
            datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(
                400, detail=f"end_condition.until is not valid ISO: {e}",
            ) from e
    return {"max_runs": max_runs, "until": until}


def _validate_action(action: Any) -> dict:
    """Validate ``action`` block (llm or shell)."""
    if not isinstance(action, dict):
        raise HTTPException(400, detail="action must be an object")
    mode = action.get("mode")
    if mode not in _VALID_ACTION_MODES:
        raise HTTPException(
            400,
            detail=f"action.mode must be one of {list(_VALID_ACTION_MODES)}",
        )
    out: dict[str, Any] = {"mode": mode}
    if mode == "llm":
        prompt = action.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(
                400, detail="action.prompt is required for mode=llm",
            )
        out["prompt"] = prompt
        agent = action.get("agent")
        if agent is not None:
            if not isinstance(agent, str) or "/" not in agent:
                raise HTTPException(
                    400,
                    detail="action.agent must be '<kind>/<lib_id>' or null",
                )
            out["agent"] = agent.strip()
        else:
            out["agent"] = None
        tools_allow = action.get("tools_allow")
        if tools_allow is not None:
            if not isinstance(tools_allow, list) or not all(
                isinstance(t, str) for t in tools_allow
            ):
                raise HTTPException(
                    400, detail="action.tools_allow must be a list of strings",
                )
        out["tools_allow"] = tools_allow
        # Optional model override. ``None`` (or empty) means "let claude-cli
        # pick its default model". Accepts short aliases (``opus`` /
        # ``sonnet`` / ``haiku``) and full model IDs (``claude-haiku-4-5``,
        # …). Only meaningful for isolated fires (destination=none/fresh);
        # via-session fires pick up the session's own model setting.
        model = action.get("model")
        if model is None or (isinstance(model, str) and not model.strip()):
            out["model"] = None
        elif isinstance(model, str):
            cleaned = model.strip().lower()
            if len(cleaned) > _MAX_MODEL_LEN or not _MODEL_RE.match(cleaned):
                raise HTTPException(
                    400,
                    detail=(
                        "action.model must be a short alias (opus/sonnet/haiku) "
                        "or a model ID like claude-haiku-4-5"
                    ),
                )
            out["model"] = cleaned
        else:
            raise HTTPException(
                400,
                detail="action.model must be a string or null",
            )
        out["command"] = None
    else:  # shell
        command = action.get("command")
        if not isinstance(command, str) or not command.strip():
            raise HTTPException(
                400, detail="action.command is required for mode=shell",
            )
        if len(command) > _MAX_COMMAND_LEN:
            raise HTTPException(
                400,
                detail=f"action.command exceeds max length ({_MAX_COMMAND_LEN} chars)",
            )
        out["command"] = command
        out["prompt"] = None
        out["agent"] = None
        out["tools_allow"] = None
        out["model"] = None
    return out


def _validate_destination(dest: Any) -> dict:
    """Validate ``destination`` block."""
    if not isinstance(dest, dict):
        raise HTTPException(400, detail="destination must be an object")
    mode = dest.get("mode")
    if mode not in _VALID_DEST_MODES:
        raise HTTPException(
            400,
            detail=f"destination.mode must be one of {list(_VALID_DEST_MODES)}",
        )
    out: dict[str, Any] = {"mode": mode}
    if mode == "existing":
        sid = dest.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise HTTPException(
                400,
                detail="destination.session_id is required for mode=existing",
            )
        out["session_id"] = sid.strip()
        out["agent"] = None
        out["rolling_session_id"] = None
    elif mode == "telegram":
        # Output goes straight to the Telegram bot via notify.notify().
        # No chat session involved — full run output renders as the
        # Telegram message (or attached file when long).
        out["agent"] = None
        out["session_id"] = None
        out["rolling_session_id"] = None
    else:
        agent = dest.get("agent")
        if agent is not None:
            if not isinstance(agent, str) or "/" not in agent:
                raise HTTPException(
                    400,
                    detail="destination.agent must be '<kind>/<lib_id>' or null",
                )
            out["agent"] = agent.strip()
        else:
            out["agent"] = None
        out["session_id"] = None
        out["rolling_session_id"] = dest.get("rolling_session_id")
    return out


def _validate_notify(notify: Any) -> dict | None:
    """Validate optional ``notify`` block.

    Shape::

        {
          "on": ["failed"],          # subset of "ok" | "failed" | "skipped" | "all"
          "priority": null,          # optional 1-5 int
          "topic": null              # optional string override; default "cron"
        }

    Returns the normalized dict or ``None`` when the caller passed ``None``.
    Rejects unknown keys / wrong types with HTTPException(400).
    """
    if notify is None:
        return None
    if not isinstance(notify, dict):
        raise HTTPException(400, detail="notify must be an object")
    allowed_keys = {"on", "priority", "topic"}
    extra = set(notify.keys()) - allowed_keys
    if extra:
        raise HTTPException(
            400,
            detail=f"notify has unknown keys: {sorted(extra)}",
        )

    on = notify.get("on")
    if on is None:
        on_norm: list[str] = ["failed"]
    else:
        if not isinstance(on, list) or not all(isinstance(s, str) for s in on):
            raise HTTPException(400, detail="notify.on must be a list of strings")
        invalid = [s for s in on if s not in _VALID_NOTIFY_ON]
        if invalid:
            raise HTTPException(
                400,
                detail=(
                    f"notify.on values must be in {list(_VALID_NOTIFY_ON)}; "
                    f"got {invalid}"
                ),
            )
        # Dedup while preserving order (stable, deterministic).
        seen: set[str] = set()
        on_norm = []
        for s in on:
            if s not in seen:
                seen.add(s)
                on_norm.append(s)

    priority = notify.get("priority")
    if priority is not None:
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise HTTPException(
                400, detail="notify.priority must be an integer 1..5 or null",
            )
        if priority < 1 or priority > 5:
            raise HTTPException(
                400, detail="notify.priority must be between 1 and 5",
            )

    topic = notify.get("topic")
    if topic is not None:
        if not isinstance(topic, str):
            raise HTTPException(400, detail="notify.topic must be a string or null")
        topic = topic.strip()
        if not topic:
            topic = None
        elif len(topic) > _NOTIFY_TOPIC_MAX_LEN:
            raise HTTPException(
                400,
                detail=(
                    f"notify.topic exceeds max length "
                    f"({_NOTIFY_TOPIC_MAX_LEN} chars)"
                ),
            )

    return {"on": on_norm, "priority": priority, "topic": topic}


def _normalize_job_payload(payload: dict, *, require_full: bool) -> dict:
    """Validate + normalize an incoming job dict.

    If ``require_full`` is True (POST), every required field must be
    present. If False (PATCH partial), only present fields are validated.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="body must be a JSON object")

    out: dict[str, Any] = {}

    if require_full or "name" in payload:
        name = _require_str(payload, "name", max_len=_MAX_NAME_LEN)
        out["name"] = name

    if "id" in payload and payload["id"] is not None:
        out["id"] = _validate_job_id(str(payload["id"]))

    if "description" in payload:
        desc = payload.get("description")
        if desc is None:
            out["description"] = None
        elif isinstance(desc, str):
            out["description"] = desc
        else:
            raise HTTPException(400, detail="description must be a string")

    if "enabled" in payload:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, detail="enabled must be a boolean")
        out["enabled"] = enabled
    elif require_full:
        out["enabled"] = True

    if require_full or "trigger" in payload:
        out["trigger"] = _validate_trigger(payload.get("trigger"))

    if "end_condition" in payload or require_full:
        out["end_condition"] = _validate_end_condition(payload.get("end_condition"))

    if require_full or "action" in payload:
        out["action"] = _validate_action(payload.get("action"))

    if require_full or "destination" in payload:
        out["destination"] = _validate_destination(payload.get("destination"))

    if "concurrency" in payload:
        conc = payload.get("concurrency")
        if conc not in _VALID_CONCURRENCY:
            raise HTTPException(
                400,
                detail=f"concurrency must be one of {list(_VALID_CONCURRENCY)}",
            )
        out["concurrency"] = conc
    elif require_full:
        out["concurrency"] = "skip"

    if "created_by" in payload:
        cb = payload.get("created_by")
        if cb is not None and not isinstance(cb, str):
            raise HTTPException(400, detail="created_by must be a string")
        out["created_by"] = cb

    if "notify" in payload:
        out["notify"] = _validate_notify(payload.get("notify"))

    return out


def _dry_build_trigger(trigger: dict) -> None:
    """Dry-build an APScheduler trigger to surface spec errors early.

    Delegates to ``cron_scheduler.compute_next_fires`` (which must raise
    ``ValueError`` on bad spec) so we don't import APScheduler from this
    module directly. If the scheduler module exposes a dedicated
    validator we prefer it.
    """
    validator = getattr(cron_scheduler, "validate_trigger", None)
    if callable(validator):
        validator(trigger)
        return
    compute = getattr(cron_scheduler, "compute_next_fires", None)
    if callable(compute):
        compute(trigger, 1)
        return
    # Last resort: skip; the scheduler will surface errors at register time.


def _summarize_runs(runs: list[dict]) -> dict[str, int]:
    """Count last-N run statuses for sparkline / list-view summary."""
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for run in runs:
        status = run.get("status") if isinstance(run, dict) else None
        if status in counts:
            counts[status] += 1
    return counts


def _enrich_job(job: dict, *, prefetched_runs: list[dict] | None = None) -> dict:
    """Add computed ``next_run_at`` + recent run summary to a job dict.

    ``prefetched_runs`` (newest-first, already limited) bypasses the per-job
    ``list_runs`` call — used by the list endpoint after one bulk read of
    ``runs.jsonl`` (see ``list_runs_grouped_by_job``).
    """
    if not isinstance(job, dict):
        return job
    out = dict(job)
    job_id = job.get("id")
    if job_id:
        if prefetched_runs is not None:
            recent = prefetched_runs
        else:
            try:
                recent = cron_store.list_runs(job_id=job_id, limit=20)
            except Exception:
                recent = []
        out["recent_runs"] = _summarize_runs(recent)
        compute = getattr(cron_scheduler, "compute_next_fires", None)
        trigger = job.get("trigger")
        if callable(compute) and isinstance(trigger, dict) and job.get("enabled"):
            try:
                fires = compute(trigger, 1)
                if fires:
                    first = fires[0]
                    out["next_run_at"] = (
                        first.isoformat() if hasattr(first, "isoformat") else first
                    )
            except Exception:
                pass
    return out


# ── route registration ────────────────────────────────────────────


def register(app: FastAPI) -> None:
    """Mount the cron routes on ``app``.

    No-op if dependent modules failed to import (logged once at module
    import time).
    """
    if not _MODULES_OK:
        _logger.warning(
            "cron_routes.register: skipped — dependent modules unavailable",
        )
        return

    # ── list / detail ─────────────────────────────────────────────

    @app.get("/api/cron/jobs")
    async def api_list_cron_jobs(agent: str | None = None) -> list[dict]:
        """List jobs. Optional ``?agent=<kind>/<lib_id>`` (or ``global``) filters
        to jobs whose ``action.agent`` OR ``destination.agent`` matches — used
        by the library detail panel to show the "Linked schedulers" section
        for an Area / Project / Resource.
        """
        try:
            jobs = await cron_store.list_jobs()
        except Exception as e:
            _logger.exception("cron_routes: list_jobs failed")
            raise _http_for(e) from e
        if isinstance(jobs, dict):
            iterable = jobs.values()
        else:
            iterable = jobs or []
        # One bulk read of runs.jsonl + group_by(job_id) instead of per-job
        # filtered list_runs (which re-parsed the whole file each time).
        # Full runs.jsonl parse — off the event loop (the Scheduler tab polls
        # this and the cache busts on every fire).
        try:
            grouped = await asyncio.to_thread(
                cron_store.list_runs_grouped_by_job, limit_per_job=20
            )
        except Exception:
            grouped = {}
        out = [
            _enrich_job(job, prefetched_runs=grouped.get(job.get("id"), []))
            for job in iterable
            if isinstance(job, dict)
        ]
        if agent:
            wanted = agent.strip().lower()
            def _matches(job: dict) -> bool:
                a = ((job.get("action") or {}).get("agent") or "").strip().lower()
                d = ((job.get("destination") or {}).get("agent") or "").strip().lower()
                # Treat empty / "global" / null as the Global agent for filter purposes.
                if wanted in ("global", "", "null"):
                    return a in ("", "global", "null") or d in ("", "global", "null")
                return a == wanted or d == wanted
            out = [j for j in out if _matches(j)]
        return out

    @app.get("/api/cron/jobs/{job_id}")
    async def api_get_cron_job(job_id: str) -> dict:
        safe = _validate_job_id(job_id)
        try:
            job = await cron_store.get_job(safe)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except Exception as e:
            _logger.exception("cron_routes: get_job failed for %s", safe)
            raise _http_for(e) from e
        if not job:
            raise HTTPException(404, detail=f"job not found: {safe}")

        try:
            runs = await asyncio.to_thread(cron_store.list_runs, job_id=safe, limit=50)
        except Exception:
            _logger.exception("cron_routes: list_runs failed for %s", safe)
            runs = []

        # Pass the runs we already fetched so _enrich_job skips its internal
        # list_runs parse — avoids a redundant file read AND keeps
        # recent_runs consistent with runs (a second read could straddle a
        # concurrent append_run and show a different snapshot). list_runs is
        # newest-first, so runs[:20] is exactly the slice _enrich_job's
        # internal limit=20 call would have summarized. compute_next_fires is
        # pure CPU, so off-load is cheap.
        enriched = await asyncio.to_thread(_enrich_job, job, prefetched_runs=runs[:20])
        enriched["runs"] = runs
        return enriched

    # ── create / patch / delete ───────────────────────────────────

    @app.post("/api/cron/jobs")
    async def api_create_cron_job(payload: dict = Body(default={})) -> dict:
        normalized = _normalize_job_payload(payload, require_full=True)

        # Auto-derive id from name if missing.
        job_id = normalized.get("id")
        if not job_id:
            try:
                job_id = cron_store.safe_job_id(normalized["name"])
            except ValueError as e:
                raise HTTPException(400, detail=str(e)) from e
        normalized["id"] = job_id

        # Validate trigger by dry-building it; surface validator message.
        try:
            _dry_build_trigger(normalized["trigger"])
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            _logger.exception("cron_routes: trigger dry-build failed")
            raise _http_for(e) from e

        # Persist via store, then register in scheduler.
        try:
            await cron_store.upsert_job(job_id, normalized)
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        except Exception as e:
            _logger.exception("cron_routes: upsert_job failed for %s", job_id)
            raise _http_for(e) from e

        try:
            await cron_scheduler.register_job(normalized)
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        except Exception as e:
            _logger.exception("cron_routes: register_job failed for %s", job_id)
            raise _http_for(e) from e

        try:
            stored = await cron_store.get_job(job_id)
        except Exception:
            stored = normalized
        return _enrich_job(stored or normalized)

    @app.patch("/api/cron/jobs/{job_id}")
    async def api_patch_cron_job(
        job_id: str, payload: dict = Body(default={}),
    ) -> dict:
        safe = _validate_job_id(job_id)
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="body must be a JSON object")

        patch = _normalize_job_payload(payload, require_full=False)

        # If trigger is being changed, dry-build first.
        if "trigger" in patch:
            try:
                _dry_build_trigger(patch["trigger"])
            except ValueError as e:
                raise HTTPException(400, detail=str(e)) from e
            except HTTPException:
                raise
            except Exception as e:
                _logger.exception("cron_routes: trigger dry-build failed")
                raise _http_for(e) from e

        try:
            updated = await cron_store.patch_job(safe, patch)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        except Exception as e:
            _logger.exception("cron_routes: patch_job failed for %s", safe)
            raise _http_for(e) from e

        # Re-register / pause-resume in scheduler if relevant.
        try:
            if "trigger" in patch:
                reschedule = getattr(cron_scheduler, "reschedule_job", None)
                if callable(reschedule):
                    await reschedule(safe, patch["trigger"])
                else:
                    await cron_scheduler.register_job(updated)
            if "enabled" in patch:
                if patch["enabled"]:
                    await cron_scheduler.resume_job(safe)
                else:
                    await cron_scheduler.pause_job(safe)
        except (FileNotFoundError, KeyError):
            # Job may not be in scheduler yet (was disabled); re-register it.
            try:
                if updated.get("enabled"):
                    await cron_scheduler.register_job(updated)
            except Exception as e:
                _logger.exception(
                    "cron_routes: re-register after patch failed for %s", safe,
                )
                raise _http_for(e) from e
        except Exception as e:
            _logger.exception("cron_routes: scheduler sync failed for %s", safe)
            raise _http_for(e) from e

        return _enrich_job(updated)

    @app.delete("/api/cron/jobs/{job_id}")
    async def api_delete_cron_job(job_id: str) -> dict:
        safe = _validate_job_id(job_id)
        # Unregister from scheduler first; missing-job is OK.
        try:
            unregister = getattr(cron_scheduler, "unregister_job", None)
            if callable(unregister):
                await unregister(safe)
            else:
                pause = getattr(cron_scheduler, "pause_job", None)
                if callable(pause):
                    try:
                        await pause(safe)
                    except (FileNotFoundError, KeyError):
                        pass
        except (FileNotFoundError, KeyError):
            pass
        except Exception as e:
            _logger.exception("cron_routes: unregister failed for %s", safe)
            raise _http_for(e) from e

        try:
            await cron_store.delete_job(safe)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except Exception as e:
            _logger.exception("cron_routes: delete_job failed for %s", safe)
            raise _http_for(e) from e

        return {"ok": True}

    # ── pause / resume / run ──────────────────────────────────────

    @app.post("/api/cron/jobs/{job_id}/pause")
    async def api_pause_cron_job(job_id: str) -> dict:
        safe = _validate_job_id(job_id)
        try:
            await cron_scheduler.pause_job(safe)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except Exception as e:
            _logger.exception("cron_routes: pause_job failed for %s", safe)
            raise _http_for(e) from e
        try:
            await cron_store.patch_job(safe, {"enabled": False})
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except Exception as e:
            _logger.exception("cron_routes: patch enabled=false failed for %s", safe)
            raise _http_for(e) from e
        return {"ok": True}

    @app.post("/api/cron/jobs/{job_id}/resume")
    async def api_resume_cron_job(job_id: str) -> dict:
        safe = _validate_job_id(job_id)
        try:
            await cron_scheduler.resume_job(safe)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except Exception as e:
            _logger.exception("cron_routes: resume_job failed for %s", safe)
            raise _http_for(e) from e
        try:
            await cron_store.patch_job(safe, {"enabled": True})
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except Exception as e:
            _logger.exception("cron_routes: patch enabled=true failed for %s", safe)
            raise _http_for(e) from e
        return {"ok": True}

    @app.post("/api/cron/jobs/{job_id}/run")
    async def api_trigger_cron_job(job_id: str) -> dict:
        safe = _validate_job_id(job_id)
        try:
            run_id = await cron_scheduler.trigger_now(safe)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"job not found: {safe}") from e
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        except Exception as e:
            _logger.exception("cron_routes: trigger_now failed for %s", safe)
            raise _http_for(e) from e
        return {"run_id": run_id}

    # ── runs ──────────────────────────────────────────────────────

    @app.get("/api/cron/runs")
    async def api_list_cron_runs(
        job_id: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        if limit < 1 or limit > 1000:
            raise HTTPException(400, detail="limit must be between 1 and 1000")
        safe_job: str | None = None
        if job_id:
            safe_job = _validate_job_id(job_id)
        # `?since=` accepts either a Unix timestamp (e.g. "1714945200" or
        # "1714945200.5") OR an ISO-8601 string (e.g. "2026-05-06T12:00:00Z").
        # cron_store.list_runs expects a float Unix timestamp, so normalise
        # before passing through.
        since_ts: float | None = None
        if since is not None and since.strip():
            raw = since.strip()
            try:
                since_ts = float(raw)
            except ValueError:
                try:
                    iso = raw.replace("Z", "+00:00")
                    since_ts = datetime.fromisoformat(iso).timestamp()
                except ValueError as e:
                    raise HTTPException(
                        400,
                        detail=f"since must be a Unix timestamp or ISO-8601 datetime: {e}",
                    ) from e
        try:
            runs = await asyncio.to_thread(
                cron_store.list_runs,
                job_id=safe_job, status=status, since_ts=since_ts, limit=limit,
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        except Exception as e:
            _logger.exception("cron_routes: list_runs failed")
            raise _http_for(e) from e
        return list(runs or [])

    @app.get("/api/cron/runs/{run_id}")
    async def api_get_cron_run(run_id: str) -> dict:
        if not isinstance(run_id, str) or not run_id.strip():
            raise HTTPException(400, detail="run_id must be a non-empty string")
        def _fetch_run() -> dict | None:
            get_run = getattr(cron_store, "get_run", None)
            if callable(get_run):
                return get_run(run_id)
            # Fallback: scan via list_runs without job filter.
            matches = [
                r for r in cron_store.list_runs(limit=1000)
                if isinstance(r, dict) and r.get("run_id") == run_id
            ]
            return matches[0] if matches else None

        try:
            # get_run / list_runs fully parse runs.jsonl — off the loop.
            run = await asyncio.to_thread(_fetch_run)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"run not found: {run_id}") from e
        except Exception as e:
            _logger.exception("cron_routes: get_run failed for %s", run_id)
            raise _http_for(e) from e
        if not run:
            raise HTTPException(404, detail=f"run not found: {run_id}")
        return run

    # ── preview (next-fires for create-modal UI) ──────────────────

    @app.post("/api/cron/preview")
    async def api_preview_cron(payload: dict = Body(default={})) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="body must be a JSON object")
        trigger = _validate_trigger(payload.get("trigger"))
        # end_condition is accepted but currently informational; UI may
        # truncate the preview list when until is in the past, etc.
        _validate_end_condition(payload.get("end_condition"))

        compute = getattr(cron_scheduler, "compute_next_fires", None)
        if not callable(compute):
            raise HTTPException(
                500, detail="cron_scheduler.compute_next_fires unavailable",
            )
        try:
            fires = compute(trigger, 5)
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        except Exception as e:
            _logger.exception("cron_routes: compute_next_fires failed")
            raise _http_for(e) from e

        out: list[str] = []
        for fire in fires or []:
            if hasattr(fire, "isoformat"):
                out.append(fire.isoformat())
            elif isinstance(fire, str):
                out.append(fire)
        return {"next_fires": out}
