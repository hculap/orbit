"""Per-process system-usage attribution for the System drill-down.

Pure-stdlib ``/proc`` reader (NO psutil) that answers the question issue #104
asks of the System cards: *which project / agent / app is generating this CPU /
RAM / disk usage?* Live processes are grouped by what they belong to:

- ``agent``   — an orchestrator claude session (resolved to its PARA project/area)
- ``project`` / ``area`` — a non-agent process whose cwd lives under ~/Projects or ~/Areas
- ``app``     — a known service comm (nginx, tailscaled, …) or a Docker container
- ``system``  — everything else
- ``other``   — the long tail rolled into one bucket so the top-N bars stay honest

Powers ``GET /api/system/processes?metric=cpu|mem|disk``.

TTL-cached with the same double-checked-locking pattern as
``system_status.all_status`` because the cpu metric sleeps ~0.4 s for a delta
sample and the disk metric walks the filesystem — neither should run more than
once per dashboard poll. The probe always runs OUTSIDE the lock.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import discovery
from . import orchestrator_meta as meta_mod
from . import orchestrator_tmux as tmux_mod
from . import share as share_mod
from . import system_status

# ── env-derived constants ──────────────────────────────────────────
try:
    _CLK_TCK: float = float(os.sysconf("SC_CLK_TCK"))
except (ValueError, OSError, AttributeError):  # pragma: no cover - exotic platforms
    _CLK_TCK = 100.0
try:
    _PAGE_SIZE: int = int(os.sysconf("SC_PAGE_SIZE"))
except (ValueError, OSError, AttributeError):  # pragma: no cover
    _PAGE_SIZE = 4096
_CORES: int = os.cpu_count() or 1
_HOME: Path = discovery.HOME

# Canonical 8-4-4-4-12 hex UUID. Used to validate both the session token in a
# claude cmdline (after ``--session-id`` / ``--resume``) and the suffix of an
# ``hd-<uuid>`` tmux session name.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Docker container id as it appears in /proc/<pid>/cgroup, e.g.
# ``0::/system.slice/docker-<64hex>.scope`` (verified live on the box).
_CGROUP_DOCKER_RE = re.compile(r"docker[-/]([0-9a-f]{64})")

# comm values we recognise as host services → kind "app". Anything not here and
# not attributable to an agent / project / container falls through to "system".
_APP_COMMS: frozenset[str] = frozenset({
    "nginx", "tailscaled", "syncthing", "ttyd", "dockerd", "containerd",
    "containerd-shim", "containerd-shim-runc-v2", "postgres", "redis-server",
    "redis", "uvicorn", "sshd", "fail2ban-server", "node", "acme.sh",
})

# How long a snapshot is reused. cpu/mem are cheap-ish but the cpu path sleeps
# ~0.4 s; disk walks the tree (seconds) and is fetched on demand, so it gets a
# much longer window matching share._folder_size's own 60 s cache.
_TTL: dict[str, float] = {"cpu": 4.0, "mem": 4.0, "disk": 60.0}

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()

# docker ps name map (full-id → name) cache — only consulted when a container
# process is actually found, refreshed at most every 30 s.
_DOCKER_NAMES: dict[str, object] = {"ts": 0.0, "map": {}}
_DOCKER_NAMES_TTL = 30.0


# ── /proc reader ────────────────────────────────────────────────────

def _parse_stat(raw: str) -> tuple[str, str, int, int] | None:
    """``(comm, state, ppid, cpu_ticks)`` from a ``/proc/<pid>/stat`` line.

    ``comm`` is wrapped in parens and may itself contain spaces or parens
    (e.g. ``(weird (proc) name)``), so split on the FIRST ``(`` and the LAST
    ``)``. Everything after the comm is a space-separated field list starting
    at ``state`` (stat field 3); ``utime``/``stime`` are fields 14/15 → indices
    11/12 of that list. Returns None on a malformed/short line.
    """
    try:
        lp = raw.index("(")
        rp = raw.rindex(")")
        comm = raw[lp + 1:rp]
        rest = raw[rp + 2:].split()
        state = rest[0]
        ppid = int(rest[1])
        utime = int(rest[11])
        stime = int(rest[12])
    except (ValueError, IndexError):
        return None
    return comm, state, ppid, utime + stime


def _read_pid(pid: int) -> dict | None:
    """Parse one ``/proc/<pid>`` entry. Returns None for kernel threads
    (no cmdline) and for races where the process exited mid-read.

    Reads ``stat`` (utime+stime ticks, ppid, comm, state), ``statm`` (RSS via
    resident pages × page size), ``cmdline`` (NUL-split argv) and best-effort
    ``cwd`` (EPERM-guarded — readable only for our own processes).
    """
    base = f"/proc/{pid}"
    try:
        with open(f"{base}/stat", "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    parsed = _parse_stat(raw)
    if parsed is None:
        return None
    comm, state, ppid, cpu_ticks = parsed

    try:
        with open(f"{base}/cmdline", "rb") as fh:
            raw_cmd = fh.read()
    except OSError:
        raw_cmd = b""
    cmdline = [c.decode("utf-8", "replace") for c in raw_cmd.split(b"\x00") if c]
    if not cmdline:
        return None  # kernel thread / exited

    rss_bytes = 0
    try:
        with open(f"{base}/statm", "r", encoding="utf-8") as fh:
            resident = int(fh.read().split()[1])
        rss_bytes = resident * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        rss_bytes = 0

    cwd: str | None = None
    try:
        cwd = os.readlink(f"{base}/cwd")
    except OSError:
        cwd = None

    return {
        "pid": pid,
        "ppid": ppid,
        "comm": comm,
        "state": state,
        "cmdline": cmdline,
        "rss_bytes": rss_bytes,
        "cpu_ticks": cpu_ticks,
        "cwd": cwd,
    }


def proc_table() -> dict[int, dict]:
    """One pass over ``/proc``. Maps ``pid -> row`` (see ``_read_pid``).

    Module-level (not nested) so the cpu sampler and unit tests can monkeypatch
    it.
    """
    table: dict[int, dict] = {}
    try:
        entries = os.scandir("/proc")
    except OSError:  # pragma: no cover - /proc always present on Linux
        return table
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            row = _read_pid(int(entry.name))
            if row is not None:
                table[row["pid"]] = row
    return table


def cpu_sample(interval: float = 0.4) -> tuple[dict[int, dict], dict[int, float]]:
    """Two ``proc_table`` snapshots ``interval`` seconds apart → per-pid CPU %.

    ``pct = (Δticks / SC_CLK_TCK / interval) * 100`` where 100 % == one fully
    busy core. Returns the *second* (current) table plus the pct map; pids that
    appeared only in the second snapshot are skipped (no baseline). This is the
    only place that sleeps — always gated behind the TTL cache.
    """
    first = proc_table()
    baseline = {pid: row["cpu_ticks"] for pid, row in first.items()}
    time.sleep(interval)
    second = proc_table()
    pct: dict[int, float] = {}
    for pid, row in second.items():
        prev = baseline.get(pid)
        if prev is None:
            continue
        delta = row["cpu_ticks"] - prev
        if delta < 0:
            delta = 0
        pct[pid] = (delta / _CLK_TCK / interval) * 100.0 if interval > 0 else 0.0
    return second, pct


# ── tmux pane → session map ─────────────────────────────────────────

def _parse_tmux_pane_output(stdout: str) -> dict[int, str]:
    """Parse ``pane_pid session_name`` lines, keeping ONLY ``hd-<uuid>`` rows.

    The hd-orch socket also carries stray non-orchestrator panes (e.g. a bare
    ``zsh`` shell shows up as session ``6``); those MUST be ignored so a random
    pane pid never gets mistaken for an agent.
    """
    out: dict[int, str] = {}
    prefix = tmux_mod.SESSION_PREFIX
    for line in stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, name = parts[0], parts[1].strip()
        if not name.startswith(prefix):
            continue
        uuid = name[len(prefix):]
        if not _UUID_RE.match(uuid):
            continue
        try:
            out[int(pid_s)] = uuid
        except ValueError:
            continue
    return out


def tmux_pid_to_session() -> dict[int, str]:
    """``{pane_pid: session_uuid}`` for live orchestrator panes. Best-effort —
    an empty map on any failure (tmux missing, socket gone)."""
    try:
        proc = subprocess.run(
            ["tmux", "-L", tmux_mod.TMUX_SOCKET, "list-panes", "-a",
             "-F", "#{pane_pid} #{session_name}"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:  # noqa: BLE001 - diagnostics never raise
        return {}
    if proc.returncode != 0:
        return {}
    return _parse_tmux_pane_output(proc.stdout)


# ── attribution ─────────────────────────────────────────────────────

def _uuid_from_cmdline(cmdline: list[str]) -> str | None:
    """Session uuid following ``--session-id`` / ``--resume`` (BOTH forms occur
    live: NEW sessions spawn with ``--session-id``, resumed ones with
    ``--resume``). Also tolerates the ``--flag=uuid`` shape defensively."""
    for i, tok in enumerate(cmdline):
        if tok in ("--session-id", "--resume"):
            if i + 1 < len(cmdline) and _UUID_RE.match(cmdline[i + 1]):
                return cmdline[i + 1]
        elif tok.startswith("--session-id=") or tok.startswith("--resume="):
            cand = tok.split("=", 1)[1]
            if _UUID_RE.match(cand):
                return cand
    return None


def _session_uuid_for(pid: int, row: dict, tmux_map: dict[int, str],
                      table: dict[int, dict]) -> str | None:
    """Resolve the orchestrator session uuid for a pid, in order:
    (a) it IS a tmux pane pid (the claude process itself);
    (b) its cmdline carries ``--session-id``/``--resume <uuid>``;
    (c) an ancestor pid is a tmux pane pid (a tool/subprocess claude spawned).
    """
    if pid in tmux_map:
        return tmux_map[pid]
    uuid = _uuid_from_cmdline(row.get("cmdline") or [])
    if uuid:
        return uuid
    seen: set[int] = set()
    cur = row.get("ppid")
    while isinstance(cur, int) and cur > 1 and cur not in seen:
        seen.add(cur)
        if cur in tmux_map:
            return tmux_map[cur]
        parent = table.get(cur)
        cur = parent.get("ppid") if parent else None
    return None


def _local_agent_name(cwd: str | None, lib_id: str | None) -> str:
    """Fallback humanizer mirroring orchestrator._agent_name_for, used only if
    importing the (heavy) orchestrator module fails. Kept tiny on purpose."""
    raw: str | None = None
    if lib_id:
        m = re.match(r"^(areas|projects)/(.+)$", lib_id)
        raw = m.group(2).split("/")[-1] if m else lib_id
    elif cwd:
        base = cwd.rstrip("/").split("/")[-1]
        if base and base.lower() not in ("", "home", Path.home().name.lower()):
            raw = base
    if not raw:
        return "Global"
    s = re.sub(r"[-_]+", " ", raw)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return " ".join(w[:1].upper() + w[1:] for w in s.split() if w) or "Global"


def _agent_label(cwd: str | None, lib_id: str | None) -> str:
    """Human label for a session/PARA item. Reuses the canonical
    ``orchestrator._agent_name_for`` (lazy import — orchestrator is always
    loaded in the running app; the local fallback keeps this importable in
    isolation)."""
    try:
        from . import orchestrator as _orch
        return _orch._agent_name_for(cwd, lib_id)
    except Exception:  # noqa: BLE001
        return _local_agent_name(cwd, lib_id)


def _para_for_cwd(cwd: str | None) -> dict | None:
    """Map a process cwd under ~/Projects/<X> or ~/Areas/<X> to a project/area
    group. Returns None for cwd outside PARA (incl. plain ~/)."""
    if not cwd:
        return None
    try:
        p = Path(cwd)
    except (TypeError, ValueError):
        return None
    for base, kind in ((discovery.PROJECTS, "project"), (discovery.AREAS, "area")):
        try:
            base_r = base.resolve()
        except OSError:
            continue
        if p == base_r or base_r in p.parents:
            try:
                rel = p.relative_to(base_r)
            except ValueError:
                continue
            name = rel.parts[0] if rel.parts else ""
            if not name:
                continue
            lib_id = f"{base.name.lower()}/{name}"
            return {
                "kind": kind,
                "key": lib_id,
                "label": _agent_label(None, lib_id),
                "cwd": str(base_r / name),
            }
    return None


def _container_id_from_cgroup(pid: int) -> str | None:
    """Full docker container id from ``/proc/<pid>/cgroup`` (cheap file read);
    None for non-container processes."""
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        return None
    m = _CGROUP_DOCKER_RE.search(txt)
    return m.group(1) if m else None


def _docker_name_map() -> dict[str, str]:
    """``{full_container_id: name}`` via ``docker ps`` (30 s cache). Best-effort
    — empty on any failure."""
    now = time.time()
    if (now - float(_DOCKER_NAMES["ts"])) < _DOCKER_NAMES_TTL and _DOCKER_NAMES["map"]:
        return _DOCKER_NAMES["map"]  # type: ignore[return-value]
    names: dict[str, str] = {}
    try:
        proc = subprocess.run(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    names[parts[0].strip()] = parts[1].strip()
    except Exception:  # noqa: BLE001
        pass
    _DOCKER_NAMES["ts"] = now
    _DOCKER_NAMES["map"] = names
    return names


def attribute_pid(pid: int, table: dict[int, dict], tmux_map: dict[int, str]) -> dict:
    """Attribute a single pid to its owning group.

    Resolution order (first match wins): agent → docker container → PARA
    project/area (by cwd) → known app comm → system. Returns a dict with at
    least ``kind``/``key``/``label`` (agents also carry ``session_id``/``cwd``).
    """
    row = table.get(pid) or {}
    comm = row.get("comm") or ""

    uuid = _session_uuid_for(pid, row, tmux_map, table)
    if uuid:
        meta = meta_mod.get_meta(uuid)
        cwd = meta.get("cwd")
        lib_id = meta.get("lib_id")
        return {
            "kind": "agent",
            "key": lib_id or f"session:{uuid}",
            "label": _agent_label(cwd, lib_id),
            "session_id": uuid,
            "cwd": cwd,
        }

    cid = _container_id_from_cgroup(pid)
    if cid:
        name = _docker_name_map().get(cid) or f"docker:{cid[:12]}"
        return {"kind": "app", "key": f"docker:{name}", "label": name}

    para = _para_for_cwd(row.get("cwd"))
    if para:
        return para

    if comm in _APP_COMMS:
        return {"kind": "app", "key": comm, "label": comm}

    return {"kind": "system", "key": comm or "?", "label": comm or "unknown"}


# ── aggregation ─────────────────────────────────────────────────────

def _cmd_short(cmdline: list[str], limit: int = 80) -> str:
    """Compact one-line command: basename of argv[0] + the rest, truncated."""
    if not cmdline:
        return ""
    head = os.path.basename(cmdline[0]) or cmdline[0]
    rest = " ".join(cmdline[1:])
    s = (head + (" " + rest if rest else "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _build(metric: str, table: dict[int, dict], value_map: dict[int, float],
           tmux_map: dict[int, str], top_n: int, ts: float) -> dict:
    """Group ``table`` rows by attribution, sum ``value_map`` per group, sort
    desc, keep ``top_n`` (each with its top-5 member procs) and roll the rest
    into one ``other`` bucket. Pure — no /proc access — so unit tests drive it
    directly with synthetic data."""
    groups: dict[tuple, dict] = {}
    for pid, row in table.items():
        value = float(value_map.get(pid, 0.0))
        attr = attribute_pid(pid, table, tmux_map)
        gkey = (attr["kind"], attr["key"])
        group = groups.get(gkey)
        if group is None:
            group = {**attr, "value": 0.0, "procs": []}
            groups[gkey] = group
        group["value"] += value
        group["procs"].append({
            "pid": pid,
            "comm": row.get("comm") or "",
            "value": value,
            "cmd_short": _cmd_short(row.get("cmdline") or []),
        })

    ordered = sorted(groups.values(), key=lambda g: g["value"], reverse=True)
    top = ordered[:top_n]
    tail = ordered[top_n:]

    is_cpu = metric == "cpu"
    for group in top:
        group["procs"].sort(key=lambda p: p["value"], reverse=True)
        group["procs"] = group["procs"][:5]
        if is_cpu:
            group["value"] = round(group["value"], 1)
            for proc in group["procs"]:
                proc["value"] = round(proc["value"], 1)

    if tail:
        tail_value = sum(g["value"] for g in tail)
        top.append({
            "kind": "other",
            "key": "other",
            "label": "Everything else",
            "value": round(tail_value, 1) if is_cpu else tail_value,
            "procs": [],
        })

    if is_cpu:
        total = {
            "value": round(sum(g["value"] for g in ordered), 1),
            "unit": "pct",
            "cores": _CORES,
        }
    else:  # mem
        mem = system_status.memory()
        total = {
            "used_bytes": mem.get("used_bytes", 0),
            "total_bytes": mem.get("total_bytes", 0),
            "unit": "bytes",
        }
    return {"metric": metric, "ts": ts, "total": total, "groups": top}


def top_consumers(metric: str, top_n: int = 12) -> dict:
    """Live cpu/mem attribution (collects /proc + tmux, then ``_build``)."""
    ts = time.time()
    tmux_map = tmux_pid_to_session()
    if metric == "cpu":
        table, pct = cpu_sample()
        value_map: dict[int, float] = pct
    else:  # mem
        table = proc_table()
        value_map = {pid: float(row.get("rss_bytes", 0)) for pid, row in table.items()}
    return _build(metric, table, value_map, tmux_map, top_n, ts)


# ── disk attribution (on-demand, no live procs) ─────────────────────

def _parse_human_bytes(text: str) -> int:
    """Parse a docker-style size string (``2.618GB``, ``8.761MB``, ``12.29kB``)
    into bytes. Docker uses decimal SI units."""
    m = re.match(r"\s*([0-9.]+)\s*([kKmMgGtT]?)i?[bB]?\s*$", text or "")
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    mult = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}[m.group(2).lower()]
    return int(value * mult)


def _docker_disk_bytes() -> int | None:
    """Total docker disk footprint. ``du`` over /var/lib/docker reports 0 for
    the unprivileged dashboard user (root-owned subtree), so go straight to the
    daemon: sum ``docker system df`` sizes (images + containers + volumes +
    build cache). Best-effort — None when docker is unavailable."""
    try:
        proc = subprocess.run(
            ["docker", "system", "df", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    total = 0
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += _parse_human_bytes(str(obj.get("Size", "")))
    return total or None


def _scan_children(base: Path, kind: str) -> list[dict]:
    """One disk row per immediate child directory of ``base`` (a PARA root)."""
    rows: list[dict] = []
    try:
        children = list(os.scandir(str(base)))
    except OSError:
        return rows
    base_slug = base.name.lower()
    for child in children:
        try:
            if not child.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        size = share_mod._folder_size(Path(child.path))
        rows.append({
            "kind": kind,
            "key": f"{base_slug}/{child.name}",
            "label": child.name,
            "value": size,
            "path": child.path,
        })
    return rows


def disk_attribution(top_n: int = 12) -> dict:
    """Sum disk usage of the dirs worth attributing — each PARA project/area,
    plus ~/.orchestrator, ~/Sync, ~/.claude, and Docker. Sorted desc, capped to
    ``top_n``. No live procs; expensive (filesystem walk) so the route fetches
    it on demand under a 60 s TTL."""
    ts = time.time()
    rows: list[dict] = []
    rows += _scan_children(discovery.PROJECTS, "project")
    rows += _scan_children(discovery.AREAS, "area")

    for path, label, key in (
        (_HOME / ".orchestrator", ".orchestrator", "orchestrator"),
        (_HOME / "Sync", "Sync", "sync"),
        (_HOME / ".claude", ".claude", "claude"),
    ):
        if path.exists():
            rows.append({
                "kind": "app",
                "key": key,
                "label": label,
                "value": share_mod._folder_size(path),
                "path": str(path),
            })

    docker_bytes = _docker_disk_bytes()
    if docker_bytes:
        rows.append({
            "kind": "app",
            "key": "docker",
            "label": "Docker",
            "value": docker_bytes,
            "path": "/var/lib/docker",
        })

    rows.sort(key=lambda r: r["value"], reverse=True)
    dsk = system_status.disk("/")
    total = {
        "used_bytes": dsk.get("used_bytes", 0),
        "total_bytes": dsk.get("total_bytes", 0),
        "unit": "bytes",
    }
    return {"metric": "disk", "ts": ts, "total": total, "groups": rows[:top_n]}


# ── TTL-cached dispatch (mirrors system_status.all_status) ──────────

def top_for(metric: str, top_n: int = 12) -> dict:
    """Public entry. Validates ``metric``, serves the TTL cache, runs the probe
    OUTSIDE the lock on a miss (so concurrent pollers don't all park on it)."""
    if metric not in ("cpu", "mem", "disk"):
        raise ValueError("metric must be cpu, mem, or disk")
    cache_key = f"{metric}:{top_n}"
    ttl = _TTL[metric]
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]
    # Probe outside the lock (cpu sleeps ~0.4 s; disk walks the tree).
    if metric == "disk":
        data = disk_attribution(top_n)
    else:
        data = top_consumers(metric, top_n)
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), data)
    return data
