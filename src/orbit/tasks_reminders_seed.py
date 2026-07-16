"""Seed the 60s reminder tick job into cron_store. Idempotent.

Soft-fails on any error — must never block app startup.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import cron_store
from . import tasks_config as tasks_config_mod

TASKS_REMINDERS_JOB_ID = "tasks-reminders"
TASKS_REMINDERS_INTERVAL_SPEC = "60s"
TASKS_REMINDERS_COMMAND = "python -m orbit tasks-reminders-tick"

_logger = logging.getLogger(__name__)


def _build_job_spec() -> dict:
    return {
        "id": TASKS_REMINDERS_JOB_ID,
        "name": "Tasks due-date reminders",
        "description": "Scan GH Project items for due-date reminders and push Telegram notifications.",
        "enabled": True,
        "trigger": {"type": "interval", "spec": TASKS_REMINDERS_INTERVAL_SPEC, "tz": "Europe/Warsaw"},
        "end_condition": {"max_runs": None, "until": None},
        "action": {
            "mode": "shell",
            "command": TASKS_REMINDERS_COMMAND,
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
        "created_by": "tasks_reminders_seed",
    }


async def _seed_async() -> None:
    cfg = tasks_config_mod.load()
    if not cfg.is_configured():
        _logger.info("tasks_reminders_seed: tasks feature disabled — skipping")
        # If a previous run seeded the job while enabled, leave it in cron_store —
        # the tick is a no-op while disabled, and the user may re-enable later.
        return
    existing = await cron_store.get_job(TASKS_REMINDERS_JOB_ID)
    if existing is not None:
        return
    await cron_store.upsert_job(TASKS_REMINDERS_JOB_ID, _build_job_spec())
    _logger.info("seeded tasks-reminders cron job")


def seed_tasks_reminders_job() -> None:
    """Sync entry — safe to call from `_lifespan` after the scheduler starts."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            asyncio.run(_seed_async())
        else:
            loop.create_task(_seed_async())
    except Exception as exc:  # noqa: BLE001
        print(f"[tasks_reminders_seed] seed failed: {exc}", file=sys.stderr)
