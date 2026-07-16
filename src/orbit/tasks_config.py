"""Tasks feature config — `tasks:` block of `config/override.yaml`.

Pure parsing + normalisation, no network. `project_node_id` may be empty on
first boot; `tasks_github` resolves and caches it lazily.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from . import config as overrides_mod

_USER_URL_RE = re.compile(r"github\.com/users/([^/]+)/projects/(\d+)")
_ORG_URL_RE = re.compile(r"github\.com/orgs/([^/]+)/projects/(\d+)")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_REMINDER_UNITS = frozenset({"min", "hour", "day"})


@dataclass(frozen=True)
class TasksConfig:
    enabled: bool
    project_url: str
    project_node_id: str
    project_owner: str
    project_owner_type: Literal["user", "org"]
    project_number: int
    default_repo: str
    area_repo_map: dict[str, str]
    proj_repo_map: dict[str, str]
    reminder_defaults: list[dict[str, Any]]
    fire_times: dict[str, str]
    grace_window_hours: int
    timezone: str

    def is_configured(self) -> bool:
        return self.enabled and bool(self.project_owner) and self.project_number > 0


_DEFAULT_REMINDERS: list[dict[str, Any]] = [
    {"kind": "at_period", "offset_days": 0, "period": "morning"},
]

_DEFAULT_FIRE_TIMES: dict[str, str] = {
    # New v2 period anchors used by `at_period` reminders and the new UI.
    "morning":   "09:00",
    "noon":      "12:00",
    "afternoon": "15:00",
    "evening":   "21:00",
    # Legacy keys preserved for back-compat with `morning_of` / `exact` kinds
    # still found in older sidecar entries. Treated as aliases for `morning`.
    "morning_of": "08:00",
    "exact":      "09:00",
}
_PERIODS = ("morning", "noon", "afternoon", "evening")


def _parse_project_url(url: str) -> tuple[Literal["user", "org"], str, int]:
    """Return (owner_type, owner, number) from a GH Project v2 URL."""
    m = _USER_URL_RE.search(url)
    if m:
        return "user", m.group(1), int(m.group(2))
    m = _ORG_URL_RE.search(url)
    if m:
        return "org", m.group(1), int(m.group(2))
    raise ValueError(f"Could not parse GitHub Project v2 URL: {url!r}")


def _normalize_reminder(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    # New v2 kinds
    if kind == "at_period":
        try:
            offset = int(raw.get("offset_days", 0))
        except (TypeError, ValueError):
            return None
        period = raw.get("period")
        if period not in _PERIODS or offset > 0 or offset < -365:
            return None
        return {"kind": "at_period", "offset_days": offset, "period": period}
    if kind == "at_time":
        try:
            offset = int(raw.get("offset_days", 0))
        except (TypeError, ValueError):
            return None
        time = raw.get("time")
        if offset > 0 or offset < -365 or not isinstance(time, str) or not _TIME_RE.match(time):
            return None
        return {"kind": "at_time", "offset_days": offset, "time": time}
    # Legacy kinds (still accepted; UI no longer offers them, scan loop handles them)
    if kind in ("morning_of", "exact"):
        return {"kind": kind}
    if kind == "before":
        try:
            value = int(raw.get("value", 0))
        except (TypeError, ValueError):
            return None
        unit = raw.get("unit")
        if unit not in _REMINDER_UNITS or value < 1 or value > 10080:
            return None
        return {"kind": "before", "value": value, "unit": unit}
    return None


def _coerce_str_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        k.strip(): v.strip()
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def load() -> TasksConfig:
    """Load and normalise the `tasks:` block from override.yaml.

    Missing/malformed entries fall back to defaults; the resulting `TasksConfig`
    is always safe to read.
    """
    overrides = overrides_mod.load_overrides()
    raw = overrides.get("tasks") if isinstance(overrides, dict) else None
    if not isinstance(raw, dict):
        raw = {}

    project_url = str(raw.get("project_url") or "")
    owner_type: Literal["user", "org"] = "user"
    owner = ""
    number = 0
    if project_url:
        try:
            owner_type, owner, number = _parse_project_url(project_url)
        except ValueError:
            pass  # leave defaults; is_configured() will be False

    raw_reminders = raw.get("reminder_defaults")
    if not isinstance(raw_reminders, list):
        raw_reminders = _DEFAULT_REMINDERS
    reminders = [r for r in (_normalize_reminder(x) for x in raw_reminders) if r is not None]
    if not reminders:
        reminders = list(_DEFAULT_REMINDERS)

    fire_times = dict(_DEFAULT_FIRE_TIMES)
    ft_raw = raw.get("fire_times")
    if isinstance(ft_raw, dict):
        for k in (*_PERIODS, "morning_of", "exact"):
            v = ft_raw.get(k)
            if isinstance(v, str) and _TIME_RE.match(v):
                fire_times[k] = v

    try:
        grace = max(0, int(raw.get("grace_window_hours", 6)))
    except (TypeError, ValueError):
        grace = 6

    tz = raw.get("timezone") or "Europe/Warsaw"
    if not isinstance(tz, str):
        tz = "Europe/Warsaw"

    return TasksConfig(
        enabled=bool(raw.get("enabled", False)),
        project_url=project_url,
        project_node_id=str(raw.get("project_node_id") or ""),
        project_owner=owner,
        project_owner_type=owner_type,
        project_number=number,
        default_repo=str(raw.get("default_repo") or ""),
        area_repo_map=_coerce_str_dict(raw.get("area_repo_map")),
        proj_repo_map=_coerce_str_dict(raw.get("proj_repo_map")),
        reminder_defaults=reminders,
        fire_times=fire_times,
        grace_window_hours=grace,
        timezone=tz,
    )


def resolve_repo(
    area_slug: str | None,
    proj_slug: str | None,
    cfg: TasksConfig,
) -> str:
    """Repo for a new issue. Priority: proj_slug → area_slug → default_repo."""
    if proj_slug:
        repo = cfg.proj_repo_map.get(proj_slug)
        if repo:
            return repo
    if area_slug:
        repo = cfg.area_repo_map.get(area_slug)
        if repo:
            return repo
    return cfg.default_repo
