"""Seed the hourly A2A maildir-GC tick job into cron_store. Idempotent.

Mirrors :mod:`tasks_reminders_seed`: a soft-failing sync entry called from the
lifespan after the scheduler starts. Gated on the ``a2a_enabled`` flag — if the
bus is disabled we skip seeding (like tasks_seed checks the tasks feature). A
previously-seeded job is left in place if the flag is later flipped off: the
tick is a cheap no-op while the maildir tree is empty.

Soft-fails on any error — must never block app startup.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import cron_store
from . import orchestrator_settings as settings_mod

A2A_GC_JOB_ID = "a2a-gc"
A2A_GC_INTERVAL_SPEC = "1h"
A2A_GC_COMMAND = "python -m orbit a2a-gc-tick"

_logger = logging.getLogger(__name__)


def _build_job_spec() -> dict:
    return {
        "id": A2A_GC_JOB_ID,
        "name": "A2A maildir GC",
        "description": "Sweep ~/.orchestrator/a2a maildirs: expire drained mail, cap inboxes, drop stranded messages.",
        "enabled": True,
        "trigger": {"type": "interval", "spec": A2A_GC_INTERVAL_SPEC, "tz": "Europe/Warsaw"},
        "end_condition": {"max_runs": None, "until": None},
        "action": {
            "mode": "shell",
            "command": A2A_GC_COMMAND,
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
        "created_by": "orchestrator_a2a_gc_seed",
    }


async def _seed_async() -> None:
    if not settings_mod.get_flag("a2a_enabled"):
        _logger.info("a2a_gc_seed: a2a disabled — skipping")
        # If a previous run seeded the job while enabled, leave it in cron_store —
        # the tick is a no-op against an empty maildir tree, and the user may
        # re-enable A2A later.
        return
    existing = await cron_store.get_job(A2A_GC_JOB_ID)
    if existing is not None:
        return
    await cron_store.upsert_job(A2A_GC_JOB_ID, _build_job_spec())
    _logger.info("seeded a2a-gc cron job")


def seed_a2a_gc_job() -> None:
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
        print(f"[orchestrator_a2a_gc_seed] seed failed: {exc}", file=sys.stderr)
