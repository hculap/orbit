"""tasks_reminders — pure scheduling + due-date scan loop.

Two responsibilities, kept separate so the math is unit-testable without any
network:

* :func:`compute_fire_time` — pure: when should reminder X fire for due Y?
* :func:`scan_and_fire`     — one sweep: read items + storage, dispatch via
  :func:`notify`, idempotently stamp delivery state.

Idempotency: every ``(issue_node_id, reminder_index)`` pair has one slot in
``fired``. Slots only clear when the user changes the reminder list or due
date (see :mod:`tasks_storage`), so restarts are safe.

Restart-after-outage: each missed slot is checked against
``cfg.grace_window_hours`` — within grace fires late; beyond grace is stamped
silently (no notify) so a multi-hour outage doesn't dump a wall of alerts on
the user when the dashboard comes back.
"""
from __future__ import annotations
from .public_url import public_link

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import notify as notify_mod
from . import tasks_config as tasks_config_mod
from . import tasks_github as tg
from . import tasks_storage as ts

_logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
_PERIOD_DEFAULTS = {"morning": "09:00", "noon": "12:00", "afternoon": "15:00", "evening": "21:00"}
_PRIORITY_TO_NTFY = {
    "P0-Critical": 5,
    "P1-Must": 4,
    "P2-Important": 3,
    "P3-Nice": 2,
    "Idea": 2,
}


@dataclass(frozen=True)
class ScanSummary:
    scanned: int
    fired: int
    skipped_stale: int
    errors: int

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "fired": self.fired,
            "skipped_stale": self.skipped_stale,
            "errors": self.errors,
        }


# ── pure compute ────────────────────────────────────────────────


def _parse_fire_time(spec: str | None, default_hh_mm: str) -> tuple[int, int]:
    raw = spec or default_hh_mm
    m = _TIME_RE.match(raw)
    if not m:
        m = _TIME_RE.match(default_hh_mm)
    if not m:
        return 9, 0
    return int(m.group(1)), int(m.group(2))


def _parse_due_date(due_iso: str) -> "date | None":
    """Parse a Date or full ISO datetime into a local date object."""
    from datetime import date as _date
    try:
        if "T" in due_iso or " " in due_iso:
            parsed = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
            return parsed.date()
        return _date.fromisoformat(due_iso)
    except ValueError:
        return None


def _resolve_anchor(
    due_iso: str,
    due_time: str | None,
    fire_times_cfg: dict[str, str],
    tz: ZoneInfo,
) -> datetime | None:
    """Anchor used by legacy `before` and `exact` kinds. Prefers the task's
    explicit `due_time` (HH:MM) over `fire_times.exact` default."""
    if "T" in due_iso or " " in due_iso:
        try:
            parsed = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
        except ValueError:
            return None
    d = _parse_due_date(due_iso)
    if d is None:
        return None
    if due_time and _TIME_RE.match(due_time):
        hh, mm = _parse_fire_time(due_time, "09:00")
    else:
        hh, mm = _parse_fire_time(fire_times_cfg.get("exact"), "09:00")
    return datetime.combine(d, time(hh, mm), tzinfo=tz)


def compute_fire_time(
    due_iso: str | None,
    reminder: dict[str, Any],
    fire_times_cfg: dict[str, str],
    tz: ZoneInfo,
    *,
    due_time: str | None = None,
) -> datetime | None:
    """Return the localized datetime a reminder should fire at, or ``None``.

    Pure function; no I/O, no clock reads. ``due_time`` is the task's
    optional 'HH:MM' anchor (only relevant for legacy `before` kind).
    """
    if not due_iso or not isinstance(reminder, dict):
        return None
    kind = reminder.get("kind")

    # New v2 kinds — explicit offset + period/time, no implicit anchor surprises.
    if kind == "at_period":
        try:
            offset = int(reminder.get("offset_days", 0))
        except (TypeError, ValueError):
            return None
        period = reminder.get("period")
        if period not in _PERIOD_DEFAULTS or offset > 0:
            return None
        d = _parse_due_date(due_iso)
        if d is None:
            return None
        d = d + timedelta(days=offset)
        hh, mm = _parse_fire_time(fire_times_cfg.get(period), _PERIOD_DEFAULTS[period])
        return datetime.combine(d, time(hh, mm), tzinfo=tz)

    if kind == "at_time":
        try:
            offset = int(reminder.get("offset_days", 0))
        except (TypeError, ValueError):
            return None
        time_str = reminder.get("time")
        if not isinstance(time_str, str) or not _TIME_RE.match(time_str) or offset > 0:
            return None
        d = _parse_due_date(due_iso)
        if d is None:
            return None
        d = d + timedelta(days=offset)
        hh, mm = _parse_fire_time(time_str, "09:00")
        return datetime.combine(d, time(hh, mm), tzinfo=tz)

    # Legacy kinds — still respected for older entries.
    if kind == "exact":
        return _resolve_anchor(due_iso, due_time, fire_times_cfg, tz)
    if kind == "morning_of":
        d = _parse_due_date(due_iso)
        if d is None:
            return None
        hh, mm = _parse_fire_time(fire_times_cfg.get("morning_of"), "08:00")
        return datetime.combine(d, time(hh, mm), tzinfo=tz)
    if kind == "before":
        try:
            value = int(reminder.get("value", 0))
        except (TypeError, ValueError):
            return None
        unit = reminder.get("unit")
        if unit not in ("min", "hour", "day") or value < 1:
            return None
        anchor = _resolve_anchor(due_iso, due_time, fire_times_cfg, tz)
        if anchor is None:
            return None
        delta = (
            timedelta(minutes=value) if unit == "min"
            else timedelta(hours=value) if unit == "hour"
            else timedelta(days=value)
        )
        return anchor - delta
    return None


def _format_lead(reminder: dict[str, Any]) -> str:
    kind = reminder.get("kind")
    if kind == "at_period":
        period = reminder.get("period", "morning")
        offset = int(reminder.get("offset_days", 0) or 0)
        if offset == 0:
            return f"{period}"
        return f"{abs(offset)}d before · {period}"
    if kind == "at_time":
        offset = int(reminder.get("offset_days", 0) or 0)
        time_str = reminder.get("time", "")
        if offset == 0:
            return f"at {time_str}"
        return f"{abs(offset)}d before · {time_str}"
    if kind == "morning_of":
        return "morning of"
    if kind == "exact":
        return "exact"
    if kind == "before":
        v = reminder.get("value")
        u = reminder.get("unit")
        suffix = {"min": "min", "hour": "h", "day": "d"}.get(u, u or "")
        return f"{v} {suffix} before"
    return "reminder"


def format_lead(reminder: dict[str, Any]) -> str:
    """Public wrapper for use by route handlers (Reminders tab labels)."""
    return _format_lead(reminder)


# ── scan / dispatch ─────────────────────────────────────────────


async def scan_and_fire(now: datetime | None = None) -> ScanSummary:
    """Single sweep across every open task with a due date.

    Called from the 60s cron tick (subprocess) or directly from tests.
    Errors on a single task never abort the whole sweep — they're counted
    and logged.
    """
    cfg = tasks_config_mod.load()
    if not cfg.is_configured():
        return ScanSummary(0, 0, 0, 0)

    tz = ZoneInfo(cfg.timezone)
    now_local = (now or datetime.now(tz)).astimezone(tz)
    grace = timedelta(hours=cfg.grace_window_hours)

    try:
        listing = await tg.list_items(cfg, force_max_age=90)
    except tg.TasksGithubError as exc:
        _logger.warning("scan_and_fire: list_items failed: %s", exc)
        return ScanSummary(0, 0, 0, 1)

    scanned = 0
    fired = 0
    skipped_stale = 0
    errors = 0

    for item in listing["items"]:
        node_id = item.get("issue_node_id")
        due = item.get("due_date")
        if not isinstance(node_id, str) or not due:
            continue
        if (item.get("state") or "OPEN").upper() != "OPEN":
            continue
        scanned += 1

        entry = ts.get_entry(node_id)
        reminders = entry.get("reminders") or []
        due_time = entry.get("due_time")
        if not reminders and not entry.get("updated_at"):
            # Lazy-seed defaults the first time we observe this due-date task.
            reminders = list(cfg.reminder_defaults)
            try:
                entry = await ts.upsert_reminders(node_id, reminders, due)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("scan_and_fire: seed failed for %s: %s", node_id, exc)
                errors += 1
                continue

        for idx, reminder in enumerate(reminders):
            if ts.is_fired(entry, idx):
                continue
            fire_at = compute_fire_time(due, reminder, cfg.fire_times, tz, due_time=due_time)
            if fire_at is None or fire_at > now_local:
                continue
            try:
                age = now_local - fire_at
                if age > grace:
                    await ts.mark_fired(node_id, idx, now_local.isoformat(timespec="seconds"))
                    skipped_stale += 1
                    continue
                await _dispatch_task_reminder(item, reminder)
                await ts.mark_fired(node_id, idx, now_local.isoformat(timespec="seconds"))
                fired += 1
            except Exception as exc:  # noqa: BLE001
                _logger.warning("scan_and_fire: dispatch failed for %s/idx=%s: %s", node_id, idx, exc)
                errors += 1

    # Standalone reminders — own loop, simpler (explicit fire_at, single fired_at slot).
    for rid, entry in ts.all_standalone().items():
        fire_at_iso = entry.get("fire_at")
        if entry.get("fired_at") or not isinstance(fire_at_iso, str):
            continue
        scanned += 1
        try:
            fire_at = datetime.fromisoformat(fire_at_iso.replace("Z", "+00:00"))
        except ValueError:
            errors += 1
            continue
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=tz)
        fire_at = fire_at.astimezone(tz)
        if fire_at > now_local:
            continue
        try:
            age = now_local - fire_at
            if age > grace:
                await ts.mark_standalone_fired(rid, now_local.isoformat(timespec="seconds"))
                skipped_stale += 1
                continue
            await _dispatch_standalone_reminder(rid, entry)
            await ts.mark_standalone_fired(rid, now_local.isoformat(timespec="seconds"))
            fired += 1
        except Exception as exc:  # noqa: BLE001
            _logger.warning("scan_and_fire: standalone dispatch failed for %s: %s", rid, exc)
            errors += 1

    return ScanSummary(scanned, fired, skipped_stale, errors)


async def _dispatch_task_reminder(item: dict[str, Any], reminder: dict[str, Any]) -> None:
    priority_label = item.get("priority")
    priority_num = _PRIORITY_TO_NTFY.get(priority_label or "", 3)
    lead = _format_lead(reminder)
    title_text = item.get("title") or "task"
    truncated = title_text if len(title_text) <= 80 else title_text[:77] + "…"
    title = f"Tasks · {priority_label or 'P?'} · {lead} · {truncated}"
    parts = []
    if item.get("due_date"):
        parts.append(f"Due: {item['due_date']}")
    if item.get("status"):
        parts.append(f"Status: {item['status']}")
    if item.get("category"):
        parts.append(f"Category: {item['category']}")
    if item.get("waiting_on"):
        parts.append(f"Waiting on: {item['waiting_on']}")
    message = "\n".join(parts) or title_text
    await notify_mod.notify(
        topic="tasks",
        message=message,
        title=title,
        priority=priority_num,
        tags=["bell", "tasks"],
        click=item.get("url"),
    )


async def _dispatch_standalone_reminder(rid: str, entry: dict[str, Any]) -> None:
    priority_label = entry.get("priority")
    priority_num = _PRIORITY_TO_NTFY.get(priority_label or "", 3)
    title_text = entry.get("title") or "Reminder"
    truncated = title_text if len(title_text) <= 80 else title_text[:77] + "…"
    title = f"Reminder · {priority_label or 'P?'} · {truncated}"
    message = entry.get("body") or title_text
    # Try to deep-link into the dashboard's Reminders tab — if no public URL,
    # public_link() returns None and notify() omits the inline-keyboard button.
    click = public_link(f"/tasks?sub=reminders&id={rid}")
    await notify_mod.notify(
        topic="tasks",
        message=message,
        title=title,
        priority=priority_num,
        tags=["bell", "reminder"],
        click=click,
    )


# ── CLI tick entry (called from __main__) ───────────────────────


def run_tick_cli() -> int:
    """Subprocess entry: ``python -m orbit tasks-reminders-tick``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        summary = asyncio.run(scan_and_fire())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **summary.to_dict()}, ensure_ascii=False))
    return 0
