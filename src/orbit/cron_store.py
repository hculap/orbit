"""Cron sidecar — JSON store for the Scheduler feature.

Two files under ``~/.orchestrator/cron/``:

- ``jobs.json``  — canonical job registry (atomic write + asyncio Lock)
- ``runs.jsonl`` — append-only run history (rolling 30-day + 100-runs/job retention)

Mirrors :mod:`orchestrator_meta` semantics: the sidecar is the source of
truth, APScheduler's MemoryJobStore is rebuilt from this on every process
start. Atomic write pattern copied from ``orchestrator_meta._atomic_write``
(see ``orchestrator_meta.py:163-191``).
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

HOME_DIR: Path = Path.home() / ".orchestrator" / "cron"
JOBS_PATH: Path = HOME_DIR / "jobs.json"
RUNS_PATH: Path = HOME_DIR / "runs.jsonl"

_RUN_RETENTION_DAYS: int = 30
_RUN_RETENTION_PER_JOB: int = 100

_SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_ID_MAX_LEN: int = 64

_lock = asyncio.Lock()
_data: dict[str, dict] | None = None


def _warn(msg: str) -> None:
    print(f"[cron_store] {msg}", file=sys.stderr)


def bootstrap() -> None:
    """Idempotent: ensure ``HOME_DIR`` exists. Tolerant of pre-existing dirs."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)


def safe_job_id(name: str) -> str:
    """Validate + return a slug suitable for a job id.

    Mirrors :func:`library._validate_name` (library.py:56) but enforces the
    APScheduler-friendly ``[a-z0-9_-]`` set: cron job ids are also used as
    APScheduler ``job_id`` and as keys in ``jobs.json``.
    """
    if not isinstance(name, str):
        raise ValueError("job id must be a string")
    candidate = name.strip().lower()
    if not candidate:
        raise ValueError("job id required")
    if len(candidate) > _SAFE_ID_MAX_LEN:
        raise ValueError(f"job id too long (max {_SAFE_ID_MAX_LEN} chars)")
    if not _SAFE_ID_RE.match(candidate):
        raise ValueError("job id must match [a-z0-9_-], start with [a-z0-9], ≤64 chars")
    return candidate


# ── jobs.json (atomic) ──────────────────────────────────────────


def _load_jobs_from_disk() -> dict[str, dict]:
    if not JOBS_PATH.exists():
        return {}
    try:
        with JOBS_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt or unreadable {JOBS_PATH.name}: {exc}; treating as empty")
        return {}
    if not isinstance(payload, dict):
        _warn(f"{JOBS_PATH.name} is not an object; treating as empty")
        return {}
    cleaned: dict[str, dict] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, dict):
            cleaned[key] = value
    return cleaned


def _ensure_loaded() -> dict[str, dict]:
    global _data
    if _data is None:
        bootstrap()
        _data = _load_jobs_from_disk()
    return _data


def _atomic_write(payload: dict[str, dict]) -> None:
    """Atomic JSON write — mirrors orchestrator_meta._atomic_write (lines 163-191)."""
    bootstrap()
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(JOBS_PATH.parent),
        prefix=".jobs.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, JOBS_PATH)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


async def list_jobs() -> dict[str, dict]:
    """Return a deep-ish copy of every job dict keyed by id."""
    async with _lock:
        data = _ensure_loaded()
        return {k: dict(v) for k, v in data.items()}


async def get_job(job_id: str) -> dict | None:
    async with _lock:
        data = _ensure_loaded()
        entry = data.get(job_id)
        return dict(entry) if entry else None


async def upsert_job(job_id: str, job: dict) -> None:
    """Replace (or insert) the full job record for ``job_id``."""
    if not isinstance(job, dict):
        raise ValueError("job must be a dict")
    async with _lock:
        data = _ensure_loaded()
        new_data = {**data, job_id: dict(job)}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data


async def delete_job(job_id: str) -> None:
    async with _lock:
        data = _ensure_loaded()
        if job_id not in data:
            return
        new_data = {k: v for k, v in data.items() if k != job_id}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data


async def patch_job(job_id: str, patch: dict) -> dict:
    """Shallow-merge ``patch`` into the existing job; returns merged record."""
    if not isinstance(patch, dict):
        raise ValueError("patch must be a dict")
    async with _lock:
        data = _ensure_loaded()
        current = data.get(job_id)
        if current is None:
            raise KeyError(f"job not found: {job_id}")
        merged = {**current, **patch}
        new_data = {**data, job_id: merged}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        return dict(merged)


# ── runs.jsonl (append-only) ────────────────────────────────────

# Serializes ALL runs.jsonl mutations against each other. append_run() runs on
# the event loop (inside async _fire_job); prune_runs() also runs on the loop;
# but reconcile_orphans() is a SYNC APScheduler job, which AsyncIOScheduler
# dispatches via run_in_executor → a WORKER THREAD. So a worker-thread
# reconcile/prune doing read-snapshot → os.replace can race an on-loop
# append_run and silently drop the appended record. This lock makes the
# read-modify-rewrite of every writer mutually exclusive. Readers stay
# lock-free — append is ≤PIPE_BUF atomic and rewrites use atomic os.replace,
# so a reader always sees a consistent file.
_RUNS_FILE_LOCK = threading.Lock()


def append_run(run: dict) -> None:
    """Append a run record to ``runs.jsonl`` via O_APPEND.

    POSIX guarantees atomicity of writes ≤PIPE_BUF for ``O_APPEND``; our
    one-line JSON records are well under that threshold (4096 bytes typical).
    Held under ``_RUNS_FILE_LOCK`` so a concurrent prune/reconcile rewrite
    (which snapshots then os.replaces the whole file) cannot drop this append.
    """
    if not isinstance(run, dict):
        raise ValueError("run must be a dict")
    bootstrap()
    line = json.dumps(run, ensure_ascii=False) + "\n"
    with _RUNS_FILE_LOCK:
        fd = os.open(str(RUNS_PATH), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def _iter_runs() -> Iterable[dict]:
    if not RUNS_PATH.exists():
        return []
    out: list[dict] = []
    try:
        with RUNS_PATH.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError as exc:
        _warn(f"cannot read {RUNS_PATH.name}: {exc}")
        return []
    return out


# Process-local cache of (parsed + deduped) runs keyed on the file's
# (mtime, size). The file is append-only via ``append_run`` (O_APPEND, no
# rewrites except in prune_runs which writes a fresh tempfile and
# os.replaces it — that flips mtime), so any change is detected by stat
# alone. Eliminates the ~70 ms full-file parse on every /api/cron/runs hit.
_RUNS_CACHE: dict = {"key": None, "deduped": None}
# list_runs / list_runs_grouped_by_job / prune_runs now run in
# asyncio.to_thread workers, so _deduped_runs_cached can be entered
# concurrently. This lock keeps the (key, deduped) pair consistent — a reader
# must never see a new key paired with the previous deduped list. Held only for
# the fast check + write, never during the file parse, so it can't stall.
_RUNS_CACHE_LOCK = threading.Lock()


def _deduped_runs_cached() -> list[dict]:
    """Cached (mtime, size)-keyed deduped runs list — newest-first sort
    is intentionally NOT done here so the cache stays cheap; callers do
    their own sort on the result slice they actually need.
    """
    try:
        st = RUNS_PATH.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        # File missing — return an empty list, and reset the cache so a
        # later append produces a fresh read.
        with _RUNS_CACHE_LOCK:
            _RUNS_CACHE["key"] = None
            _RUNS_CACHE["deduped"] = None
        return []
    with _RUNS_CACHE_LOCK:
        if _RUNS_CACHE["key"] == key and _RUNS_CACHE["deduped"] is not None:
            return _RUNS_CACHE["deduped"]
    # Parse outside the lock (the slow part); concurrent cold callers may each
    # parse once — benign redundant work, never a torn cache pair.
    deduped = _dedupe_runs(_iter_runs())
    with _RUNS_CACHE_LOCK:
        _RUNS_CACHE["key"] = key
        _RUNS_CACHE["deduped"] = deduped
    return deduped


def _dedupe_runs(runs: Iterable[dict]) -> list[dict]:
    """Collapse duplicate run records sharing the same ``run_id``.

    ``_fire_job`` writes a placeholder ``status="running"`` record before the
    action runs, then a final record with the same ``run_id`` once it
    completes. We always prefer the final record (non-null ``finished_at``)
    over the placeholder. If only the placeholder exists (orchestrator
    crashed mid-fire), it's kept so reconcile_orphans can mark it cancelled
    on next startup.
    """
    by_id: dict[str, dict] = {}
    for rec in runs:
        rid = rec.get("run_id")
        if not rid:
            # Records without run_id (legacy / corrupt) — keep one per identity-like key.
            by_id.setdefault(f"_anon_{id(rec)}", rec)
            continue
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = rec
            continue
        existing_done = existing.get("finished_at") is not None
        rec_done = rec.get("finished_at") is not None
        if rec_done and not existing_done:
            by_id[rid] = rec
        elif rec_done and existing_done:
            # Both finalised — last write wins (covers the rare "manual run after
            # initial completion under the same run_id" edge case).
            if float(rec.get("finished_at") or 0.0) >= float(existing.get("finished_at") or 0.0):
                by_id[rid] = rec
    return list(by_id.values())


def list_runs(
    *,
    job_id: str | None = None,
    limit: int = 50,
    status: str | None = None,
    since_ts: float | None = None,
) -> list[dict]:
    """Return up to ``limit`` most-recent runs, newest first.

    Filters are AND-ed: ``job_id`` matches exact id; ``status`` matches the
    run record's ``status`` field; ``since_ts`` keeps records with
    ``started_at >= since_ts``. Records sharing a ``run_id`` are deduped —
    final records (non-null ``finished_at``) win over the placeholder
    ``status="running"`` written before the action ran.

    Uses the in-memory dedup cache (keyed on file mtime+size); successive
    calls within a tick reuse the same parse without re-reading the file.
    """
    runs = list(_deduped_runs_cached())
    if job_id is not None:
        runs = [r for r in runs if r.get("job_id") == job_id]
    if status is not None:
        runs = [r for r in runs if r.get("status") == status]
    if since_ts is not None:
        runs = [r for r in runs if float(r.get("started_at") or 0.0) >= float(since_ts)]
    runs.sort(key=lambda r: float(r.get("started_at") or 0.0), reverse=True)
    if limit and limit > 0:
        runs = runs[: int(limit)]
    return runs


def list_runs_grouped_by_job(*, limit_per_job: int = 20) -> dict[str, list[dict]]:
    """One-pass version of ``list_runs`` for callers needing every job's recent
    runs at once (e.g. ``GET /api/cron/jobs``).

    Reads ``runs.jsonl`` exactly once, dedupes records, groups by ``job_id``,
    sorts each group newest-first, and truncates to ``limit_per_job``.
    Equivalent to calling ``list_runs(job_id=X, limit=limit_per_job)`` for
    every job — but at O(file) cost instead of O(jobs × file).
    """
    grouped: dict[str, list[dict]] = {}
    for rec in _deduped_runs_cached():
        jid = rec.get("job_id")
        if not isinstance(jid, str) or not jid:
            continue
        grouped.setdefault(jid, []).append(rec)
    for jid, recs in grouped.items():
        recs.sort(key=lambda r: float(r.get("started_at") or 0.0), reverse=True)
        if limit_per_job and limit_per_job > 0:
            grouped[jid] = recs[: int(limit_per_job)]
    return grouped


def get_run(run_id: str) -> dict | None:
    """Return the final record for ``run_id``, or the running placeholder
    if no final exists yet (orchestrator may have crashed mid-fire).

    Uses the deduped cache — by construction ``_dedupe_runs`` already
    prefers final over placeholder for the same ``run_id``, so we can do
    a single dict-lookup-shaped pass instead of re-iterating the whole
    file every time the run-detail panel is opened.
    """
    for rec in _deduped_runs_cached():
        if rec.get("run_id") == run_id:
            return dict(rec)
    return None


def prune_runs() -> int:
    """Trim ``runs.jsonl`` to the rolling retention window. Returns dropped count.

    Keeps the union of: records newer than 30 days, AND the last 100 records
    per job. Whichever is larger wins for any given job — gives high-traffic
    jobs bounded history, low-traffic jobs at least a month of visibility.
    """
    if not RUNS_PATH.exists():
        return 0
    # Hold the file lock across the snapshot → rewrite so a concurrent
    # append_run (or a worker-thread reconcile rewrite) cannot land between our
    # read and our os.replace and be silently dropped.
    with _RUNS_FILE_LOCK:
        records = list(_iter_runs())
        if not records:
            return 0
        now_ts = time.time()
        cutoff_ts = now_ts - (_RUN_RETENTION_DAYS * 86400.0)
        by_job: dict[str, list[dict]] = {}
        for rec in records:
            by_job.setdefault(rec.get("job_id") or "", []).append(rec)
        keep_ids: set[int] = set()
        for _job_id, job_runs in by_job.items():
            job_runs.sort(key=lambda r: float(r.get("started_at") or 0.0), reverse=True)
            recent = job_runs[:_RUN_RETENTION_PER_JOB]
            for rec in recent:
                keep_ids.add(id(rec))
            for rec in job_runs:
                if float(rec.get("started_at") or 0.0) >= cutoff_ts:
                    keep_ids.add(id(rec))
        kept = [rec for rec in records if id(rec) in keep_ids]
        dropped = len(records) - len(kept)
        if dropped <= 0:
            return 0
        kept.sort(key=lambda r: float(r.get("started_at") or 0.0))
        _rewrite_runs(kept)
        return dropped


def _rewrite_runs(records: list[dict]) -> None:
    """Atomic rewrite of ``runs.jsonl`` — used by the pruner only."""
    bootstrap()
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(RUNS_PATH.parent),
        prefix=".runs.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        for rec in records:
            tmp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, RUNS_PATH)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


# ── alerts session sidecar (for cron_alerts) ─────────────────────


_ALERTS_META_PATH: Path = HOME_DIR / "alerts_meta.json"


def get_alerts_session_id() -> str | None:
    """Return the persisted Global 'Cron alerts' session id, or None."""
    if not _ALERTS_META_PATH.exists():
        return None
    try:
        with _ALERTS_META_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) and sid else None


def set_alerts_session_id(session_id: str) -> None:
    """Persist the alerts session id atomically."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    bootstrap()
    payload: dict[str, Any] = {"session_id": session_id.strip()}
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(_ALERTS_META_PATH.parent),
        prefix=".alerts_meta.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, _ALERTS_META_PATH)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
