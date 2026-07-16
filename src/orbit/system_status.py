"""Lightweight system metrics — read from /proc, systemctl, tailscale CLI."""
from __future__ import annotations
import concurrent.futures as futures
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

# Services we care about (per-system + per-user). Edit via override.yaml in v2.
DEFAULT_UNITS_SYSTEM = [
    "ssh",
    "nginx",
    "tailscaled",
    "orbit",
]
DEFAULT_UNITS_USER = [
    "syncthing",
    "sync-cleaner.timer",
]


def _read_proc(path: str) -> str:
    try:
        return Path(path).read_text()
    except Exception:
        return ""


def cpu_load() -> dict:
    """Load average + CPU count."""
    text = _read_proc("/proc/loadavg")
    parts = text.split()
    cpu_count = 0
    try:
        cpu_count = sum(1 for _ in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"))
    except Exception:
        pass
    return {
        "load_1m": float(parts[0]) if len(parts) > 0 else 0.0,
        "load_5m": float(parts[1]) if len(parts) > 1 else 0.0,
        "load_15m": float(parts[2]) if len(parts) > 2 else 0.0,
        "cpu_count": cpu_count,
    }


def memory() -> dict:
    """RAM and swap usage from /proc/meminfo (KB → bytes)."""
    text = _read_proc("/proc/meminfo")
    info: dict = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if not rest:
            continue
        toks = rest.strip().split()
        try:
            kb = int(toks[0])
        except ValueError:
            continue
        info[key.strip()] = kb * 1024
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent": (used / total * 100) if total else 0,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free,
        "swap_percent": ((swap_total - swap_free) / swap_total * 100) if swap_total else 0,
    }


def disk(path: str = "/") -> dict:
    try:
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": usage.used / usage.total * 100,
        }
    except Exception:
        return {"path": path, "error": "unavailable"}


def _user_systemctl_env() -> dict[str, str]:
    """Env vars `systemctl --user` needs to find the user manager bus.

    The dashboard's systemd unit sets ``User=<operator>`` but doesn't bind to the
    user@<uid>.service login session, so its inherited env lacks
    ``XDG_RUNTIME_DIR``. Without it, ``systemctl --user`` cannot reach the
    user bus and silently returns an empty stdout — every user unit ends up
    reported as inactive/unknown.
    """
    import os
    uid = os.getuid()
    runtime_dir = f"/run/user/{uid}"
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": runtime_dir,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
    }


def _systemctl_batch_status(units: list[str], user: bool = False) -> list[dict]:
    """One ``systemctl show`` call for many units; parses ActiveState +
    UnitFileState per record.

    Replaces the old per-unit is-active + is-enabled pair (2 subprocess
    forks × N units = 2N forks). One ``systemctl show -p ActiveState
    -p UnitFileState <unit1> <unit2> …`` returns blank-line-separated
    records in argument order; we emit one dict per unit in that order.
    """
    scope = "user" if user else "system"
    if not units:
        return []
    args = ["systemctl"]
    env = None
    if user:
        args.append("--user")
        env = _user_systemctl_env()
    try:
        proc = subprocess.run(
            [*args, "show", "-p", "ActiveState", "-p", "UnitFileState", *units],
            capture_output=True, text=True, timeout=3, env=env,
        )
    except Exception as e:
        return [
            {"unit": u, "scope": scope, "active": "unknown", "enabled": "unknown", "error": str(e)}
            for u in units
        ]
    # ``systemctl show`` emits one record per unit separated by a blank line.
    # Each record is KEY=VALUE lines. Order matches the argument order.
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, _, val = line.partition("=")
        if key:
            current[key.strip()] = val.strip()
    if current:
        records.append(current)
    out: list[dict] = []
    for unit, rec in zip(units, records):
        active = rec.get("ActiveState") or "unknown"
        enabled = rec.get("UnitFileState") or "unknown"
        out.append({"unit": unit, "scope": scope, "active": active, "enabled": enabled})
    # If systemctl returned fewer records than units (rare, but possible on
    # malformed input), surface the gap as 'unknown' so the UI still renders.
    for unit in units[len(records):]:
        out.append({"unit": unit, "scope": scope, "active": "unknown", "enabled": "unknown"})
    return out


def services(units_system: list[str] | None = None, units_user: list[str] | None = None) -> list[dict]:
    units_system = units_system or DEFAULT_UNITS_SYSTEM
    units_user = units_user or DEFAULT_UNITS_USER
    return (
        _systemctl_batch_status(units_system, user=False)
        + _systemctl_batch_status(units_user, user=True)
    )


# /api/system response cache. The dashboard polls this every few seconds for
# the live header + System tab. The underlying probes (systemctl + tailscale
# + /proc reads) take 100–400 ms cold; nothing here changes faster than the
# poll interval, so a short TTL is safe.
_STATUS_CACHE: dict = {"data": None, "ts": 0.0}
_STATUS_TTL_S: float = 3.0
# all_status now runs in asyncio.to_thread worker threads (see app.api_system),
# so concurrent /api/system polls can race on the cache. This threading.Lock
# (NOT asyncio.Lock — we're off the event loop) guards only the fast cache
# read-check and the two-key write so they stay consistent; it is deliberately
# NOT held across the ~3 s probe (see all_status).
_STATUS_CACHE_LOCK = threading.Lock()


def tailscale() -> dict:
    """Parse `tailscale status --json`. Returns simplified structure."""
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip()[:200]}
        data = json.loads(proc.stdout)
    except FileNotFoundError:
        return {"error": "tailscale not installed"}
    except Exception as e:
        return {"error": str(e)[:200]}

    self_info = data.get("Self", {}) or {}
    peers_dict = data.get("Peer", {}) or {}
    peers = []
    for peer in peers_dict.values():
        peers.append({
            "hostname": peer.get("HostName"),
            "dns_name": (peer.get("DNSName") or "").rstrip("."),
            "online": peer.get("Online", False),
            "os": peer.get("OS"),
            "tailscale_ip": (peer.get("TailscaleIPs") or [None])[0],
            "rx_bytes": peer.get("RxBytes", 0),
            "tx_bytes": peer.get("TxBytes", 0),
            "last_seen": peer.get("LastSeen"),
        })
    peers.sort(key=lambda p: (not p["online"], p["hostname"] or ""))
    return {
        "self_dns": (self_info.get("DNSName") or "").rstrip("."),
        "self_ip": (self_info.get("TailscaleIPs") or [None])[0],
        "magicdns": data.get("MagicDNSSuffix"),
        "peers": peers,
        "peers_total": len(peers),
        "peers_online": sum(1 for p in peers if p["online"]),
    }


def all_status(*, force_refresh: bool = False) -> dict:
    """Full system snapshot — call this from /api/system.

    Cached for ``_STATUS_TTL_S`` seconds because the dashboard polls this
    on a short interval and the probes (systemctl + tailscale) are the
    expensive part. Pass ``force_refresh=True`` to bypass the cache.
    """
    # Double-checked locking. The lock is held only for the fast cache
    # read-check and the fast two-key write — NEVER across the ~3 s probe.
    # Holding it during the probe would park every concurrent /api/system
    # worker on the lock for 3 s, starving the shared asyncio threadpool that
    # all other to_thread work (logs, git, thumbnails) also uses. Two
    # overlapping cold misses may each probe (rare, benign redundant work); the
    # two-key write stays consistent because it happens under the lock.
    now = time.time()
    with _STATUS_CACHE_LOCK:
        cached = _STATUS_CACHE.get("data")
        if (
            not force_refresh
            and cached is not None
            and (now - float(_STATUS_CACHE.get("ts") or 0.0)) < _STATUS_TTL_S
        ):
            return cached
    # The three subprocess-backed probes — systemctl (system units), systemctl
    # --user (user units), and `tailscale status` — are independent and each
    # can block up to its 3 s timeout. Run them CONCURRENTLY in a small
    # threadpool so a cold snapshot costs ~max(3 s) instead of ~sum(9 s). The
    # cheap /proc + shutil reads (cpu/memory/disk) stay inline. Probe OUTSIDE
    # the lock.
    with futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_sys = pool.submit(_systemctl_batch_status, DEFAULT_UNITS_SYSTEM, False)
        f_usr = pool.submit(_systemctl_batch_status, DEFAULT_UNITS_USER, True)
        f_ts = pool.submit(tailscale)
        # Preserve the original ordering: system units then user units, exactly
        # as the sequential services() call produced.
        svcs = [*f_sys.result(), *f_usr.result()]
        ts = f_ts.result()
    snapshot = {
        "ts": now,
        "cpu": cpu_load(),
        "memory": memory(),
        "disk_root": disk("/"),
        "services": svcs,
        "tailscale": ts,
    }
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE["data"] = snapshot
        _STATUS_CACHE["ts"] = now
    return snapshot
