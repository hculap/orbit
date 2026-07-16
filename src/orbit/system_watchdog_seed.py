"""Seed cron job that runs ``system_watchdog.check_all`` every 5 minutes.

Idempotent — only inserts the job if it isn't already in ``cron_store``. The
existing scheduler picks it up on next boot via :func:`cron_scheduler.start`,
which iterates ``store.list_jobs()`` and registers each entry; a freshly
seeded job will be registered on the next process restart.

Soft-fails on any error: this seed must never block app startup.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import cron_store

WATCHDOG_JOB_ID: str = "system-watchdog"
WATCHDOG_INTERVAL_SPEC: str = "5m"
WATCHDOG_COMMAND: str = "python -m orbit system-check"

_logger = logging.getLogger(__name__)


def _build_job_spec() -> dict:
    return {
        "id": WATCHDOG_JOB_ID,
        "name": "System watchdog",
        "description": "Periodic disk/services/TLS/tailnet checks → push notify on transitions.",
        "enabled": True,
        "trigger": {"type": "interval", "spec": WATCHDOG_INTERVAL_SPEC, "tz": "Europe/Warsaw"},
        "end_condition": {"max_runs": None, "until": None},
        "action": {
            "mode": "shell",
            "command": WATCHDOG_COMMAND,
            "prompt": None,
            "agent": None,
            "tools_allow": None,
        },
        "destination": {
            "mode": "none",
            "session_id": None,
            "agent": None,
            "rolling_session_id": None,
        },
        "concurrency": "skip",
        "created_by": "system_watchdog_seed",
    }


async def _seed_async() -> None:
    existing = await cron_store.get_job(WATCHDOG_JOB_ID)
    if existing is not None:
        return
    await cron_store.upsert_job(WATCHDOG_JOB_ID, _build_job_spec())
    _logger.info("seeded watchdog cron job: %s", WATCHDOG_JOB_ID)


def seed_watchdog_job() -> None:
    """Idempotently register the system-watchdog cron job.

    Safe to call from synchronous startup hooks: dispatches to a fresh asyncio
    loop when no loop is running, otherwise schedules a background task.
    """
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            asyncio.run(_seed_async())
        else:
            loop.create_task(_seed_async())
    except Exception as exc:  # noqa: BLE001 — never break app startup
        print(f"[system_watchdog_seed] seed failed: {exc}", file=sys.stderr)
