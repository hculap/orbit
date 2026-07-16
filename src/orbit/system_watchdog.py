"""Periodic system health checks emitting transition events.

The watchdog runs out of cron and only fires events on EDGES (transition
into a bad state), not while the bad state is held — see the state file
described below.

State persistence
-----------------
File: ``~/.orchestrator/system_watchdog_state.json`` (mode 0600). Atomic
write via ``tempfile.mkstemp`` + ``os.replace`` (mirrors
``library.write_sidecar``). Recoverable from missing / malformed file —
treats it as "fresh start" and may re-fire one round of events.

Schema (all keys optional, additive):

.. code-block:: json

    {
      "version": 1,
      "last_check_iso": "2026-05-09T07:12:00Z",
      "disk_root_severity": "ok" | "warning" | "critical",
      "memory_severity": "ok" | "warning",
      "services": {"nginx": "active" | "failed" | "unknown", ...},
      "tls_warned_at": {"/etc/letsencrypt/.../fullchain.pem": "2026-...Z"},
      "tailnet_online": {"<peer-id>": true | false},
      "last_uptime_s": 12345.6
    }

Public API
----------
* :func:`check_all` — run every check, return ``list[Event]`` of edges.
  Updates and persists state as a side effect.
* :func:`load_state` / :func:`save_state` — exposed for tests.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal, TypedDict

_logger = logging.getLogger(__name__)


class Event(TypedDict, total=False):
    severity: Literal["info", "warning", "critical"]
    type: str
    message: str
    context: dict


HOME = Path(os.path.expanduser("~"))
STATE_DIR = HOME / ".orchestrator"
STATE_PATH = STATE_DIR / "system_watchdog_state.json"

DISK_WARNING_PCT = 85.0
DISK_CRITICAL_PCT = 95.0
MEMORY_WARNING_PCT = 90.0
TLS_WARNING_DAYS = 14
UPTIME_RESET_TOLERANCE_S = 300

DEFAULT_SERVICES = ("orbit", "tailscaled", "nginx", "ssh")


def _default_cert_paths() -> tuple[str, ...]:
    """TLS certs to watch: ``ORBIT_TLS_CERT_PATHS`` (colon-separated) if set,
    else every Let's Encrypt live cert on the box, else nothing (skip the check).

    No domain is hardcoded — a fresh install with no certs simply skips TLS
    monitoring instead of watching a path that will never exist.
    """
    override = os.environ.get("ORBIT_TLS_CERT_PATHS", "").strip()
    if override:
        return tuple(p for p in override.split(":") if p)
    return tuple(sorted(str(p) for p in Path("/etc/letsencrypt/live").glob("*/fullchain.pem")))


DEFAULT_CERT_PATHS = _default_cert_paths()

_STATE_VERSION = 1


# ── state I/O ─────────────────────────────────────────────────────


def _empty_state() -> dict:
    return {
        "version": _STATE_VERSION,
        "last_check_iso": None,
        "disk_root_severity": "ok",
        "memory_severity": "ok",
        "services": {},
        "tls_warned_at": {},
        "tailnet_online": {},
        "last_uptime_s": None,
    }


def load_state(path: Path = STATE_PATH) -> dict:
    """Read state file; return a fresh dict on missing / malformed file."""
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _logger.warning("system_watchdog: state file unreadable, resetting: %s", exc)
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    base = _empty_state()
    base.update({k: v for k, v in data.items() if k in base})
    return base


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    """Atomic write to ``path`` with mode 0600. Parent created if missing."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".system_watchdog.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ── individual checks ─────────────────────────────────────────────


def _disk_severity(percent: float) -> str:
    if percent >= DISK_CRITICAL_PCT:
        return "critical"
    if percent >= DISK_WARNING_PCT:
        return "warning"
    return "ok"


def check_disk(state: dict, *, path: str = "/") -> list[Event]:
    try:
        usage = shutil.disk_usage(path)
    except Exception as exc:
        _logger.warning("system_watchdog: disk usage read failed: %s", exc)
        return []
    percent = usage.used / usage.total * 100 if usage.total else 0.0
    new_sev = _disk_severity(percent)
    prev_sev = state.get("disk_root_severity", "ok")
    state["disk_root_severity"] = new_sev
    if new_sev == prev_sev or new_sev == "ok":
        return []
    return [{
        "severity": "critical" if new_sev == "critical" else "warning",
        "type": "disk",
        "message": f"disk {path} at {percent:.1f}% ({new_sev})",
        "context": {"path": path, "percent": round(percent, 2),
                    "previous": prev_sev, "current": new_sev},
    }]


def _memory_percent() -> float | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except Exception:
        return None
    info: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        toks = rest.strip().split()
        if not toks:
            continue
        try:
            info[key.strip()] = int(toks[0])
        except ValueError:
            continue
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    if not total:
        return None
    return (total - available) / total * 100


def check_memory(state: dict) -> list[Event]:
    percent = _memory_percent()
    if percent is None:
        return []
    new_sev = "warning" if percent >= MEMORY_WARNING_PCT else "ok"
    prev_sev = state.get("memory_severity", "ok")
    state["memory_severity"] = new_sev
    if new_sev == prev_sev or new_sev == "ok":
        return []
    return [{
        "severity": "warning",
        "type": "memory",
        "message": f"memory at {percent:.1f}% (>= {MEMORY_WARNING_PCT}%)",
        "context": {"percent": round(percent, 2),
                    "previous": prev_sev, "current": new_sev},
    }]


def _systemctl_active(unit: str) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=2,
        )
    except FileNotFoundError:
        return "unknown"
    except Exception:
        return "unknown"
    return (proc.stdout or "").strip() or "unknown"


def check_services(state: dict, *, units: tuple[str, ...] = DEFAULT_SERVICES) -> list[Event]:
    prev = dict(state.get("services") or {})
    current: dict[str, str] = {}
    events: list[Event] = []
    for unit in units:
        active = _systemctl_active(unit)
        current[unit] = active
        was_ok = prev.get(unit) == "active"
        is_ok = active == "active"
        if active == "unknown":
            continue
        if was_ok and not is_ok:
            events.append({
                "severity": "critical",
                "type": "service",
                "message": f"service {unit} is {active}",
                "context": {"unit": unit, "previous": prev.get(unit), "current": active},
            })
        elif not was_ok and is_ok and unit in prev:
            events.append({
                "severity": "info",
                "type": "service",
                "message": f"service {unit} recovered",
                "context": {"unit": unit, "previous": prev.get(unit), "current": active},
            })
    state["services"] = current
    return events


def _cert_expiry_days(cert_path: str) -> float | None:
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", cert_path],
            capture_output=True, text=True, timeout=3,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip()
    if not line.startswith("notAfter="):
        return None
    raw = line[len("notAfter="):]
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y GMT"):
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return (dt.timestamp() - time.time()) / 86400.0
        except ValueError:
            continue
    return None


def check_tls(state: dict, *, cert_paths: tuple[str, ...] = DEFAULT_CERT_PATHS) -> list[Event]:
    warned = dict(state.get("tls_warned_at") or {})
    events: list[Event] = []
    for cert in cert_paths:
        if not Path(cert).is_file():
            continue
        days = _cert_expiry_days(cert)
        if days is None:
            continue
        if days < TLS_WARNING_DAYS and cert not in warned:
            warned[cert] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            events.append({
                "severity": "warning",
                "type": "tls",
                "message": f"TLS cert expires in {days:.1f}d: {cert}",
                "context": {"cert": cert, "days_left": round(days, 2)},
            })
        elif days >= TLS_WARNING_DAYS and cert in warned:
            warned.pop(cert, None)
    state["tls_warned_at"] = warned
    return events


def _tailnet_peers() -> dict[str, bool] | None:
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return None
    peers = data.get("Peer") or {}
    out: dict[str, bool] = {}
    for peer_id, peer in peers.items():
        if not isinstance(peer, dict):
            continue
        out[str(peer_id)] = bool(peer.get("Online", False))
    return out


def check_tailnet(state: dict) -> list[Event]:
    peers = _tailnet_peers()
    if peers is None:
        return []
    prev = dict(state.get("tailnet_online") or {})
    events: list[Event] = []
    for peer_id, online in peers.items():
        if peer_id in prev and prev[peer_id] and not online:
            events.append({
                "severity": "info",
                "type": "tailnet",
                "message": f"tailnet peer {peer_id} went offline",
                "context": {"peer": peer_id, "previous": True, "current": False},
            })
    state["tailnet_online"] = peers
    return events


def _read_uptime_s() -> float | None:
    try:
        text = Path("/proc/uptime").read_text(encoding="utf-8")
    except Exception:
        return None
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def check_uptime(state: dict) -> list[Event]:
    current = _read_uptime_s()
    if current is None:
        return []
    previous = state.get("last_uptime_s")
    state["last_uptime_s"] = current
    if not isinstance(previous, (int, float)):
        return []
    if current + UPTIME_RESET_TOLERANCE_S < previous:
        return [{
            "severity": "critical",
            "type": "uptime",
            "message": f"uptime reset (previous {previous:.0f}s, now {current:.0f}s)",
            "context": {"previous_s": previous, "current_s": current},
        }]
    return []


# ── orchestrator ──────────────────────────────────────────────────


def check_all(*, state_path: Path = STATE_PATH, persist: bool = True) -> list[Event]:
    """Run every check, returning the list of new edge events.

    Mutates and (by default) persists state.
    """
    state = load_state(state_path)
    events: list[Event] = []
    events.extend(check_disk(state))
    events.extend(check_memory(state))
    events.extend(check_services(state))
    events.extend(check_tls(state))
    events.extend(check_tailnet(state))
    events.extend(check_uptime(state))
    state["last_check_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if persist:
        try:
            save_state(state, state_path)
        except Exception as exc:
            _logger.warning("system_watchdog: failed to persist state: %s", exc)
    return events
