"""Tests for ``reconcile_orphans`` in cron_scheduler.

Regression 2026-05-31: my-project-decide runs that completed in 20s were
displayed as 200-700 min "cancelled" entries on the scheduler page. Root
cause: each fire writes TWO records to runs.jsonl (a `started` placeholder
without finished_at, then a `final` record on subprocess exit). On the next
orchestrator restart reconcile_orphans patched the leftover `started`
record to ``status=cancelled, finished_at=time.time()``. The dedup helper
in cron_store prefers the latest ``finished_at`` between two records with
the same run_id — so the cancelled patch (stamped HOURS after the real
finish) won over the real ``ok`` final record.

Fix: when reconcile encounters a started-without-finish record that has a
sibling FINAL record (same run_id, finished_at != None), drop the orphan
instead of patching — the final record is already authoritative.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from orbit import cron_scheduler, cron_store


@pytest.fixture
def isolated_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point cron_store at a temporary runs.jsonl so tests don't touch the
    real ``~/.orchestrator/cron/runs.jsonl``."""
    runs = tmp_path / "runs.jsonl"
    monkeypatch.setattr(cron_store, "RUNS_PATH", runs)
    monkeypatch.setattr(cron_store, "HOME_DIR", tmp_path)
    cron_store._RUNS_CACHE["key"] = None
    cron_store._RUNS_CACHE["deduped"] = None
    yield runs
    cron_store._RUNS_CACHE["key"] = None
    cron_store._RUNS_CACHE["deduped"] = None


def _started_record(run_id: str, started_at: float) -> dict:
    return {
        "run_id": run_id,
        "job_id": "my-project-decide",
        "started_at": started_at,
        "finished_at": None,
        "duration_ms": None,
        "status": "running",
        "trigger": "scheduled",
        "session_id": None,
        "exit_code": None,
        "stderr_tail": "",
        "error": None,
    }


def _final_record(run_id: str, started_at: float, duration_ms: int = 20_000) -> dict:
    return {
        "run_id": run_id,
        "job_id": "my-project-decide",
        "started_at": started_at,
        "finished_at": started_at + duration_ms / 1000.0,
        "duration_ms": duration_ms,
        "status": "ok",
        "trigger": "scheduled",
        "session_id": None,
        "exit_code": 0,
        "stderr_tail": "",
        "error": None,
        "output_preview": "My Project done\n",
    }


def test_reconcile_drops_started_when_final_exists(isolated_runs: Path) -> None:
    """The 2026-05-31 bug repro: started + final + restart-triggered reconcile
    must NOT leave a phantom 'cancelled' record for a run that actually
    finished successfully."""
    long_ago = time.time() - 6 * 3600  # 6h ago — well past the 5-min cutoff
    run_id = "fdf30e50-932b-4034-8fc0-dadaf8eee80b"

    cron_store.append_run(_started_record(run_id, long_ago))
    cron_store.append_run(_final_record(run_id, long_ago))

    cron_scheduler.reconcile_orphans()

    records = list(cron_store._iter_runs())
    # Started orphan dropped; only the authoritative final record remains.
    statuses = [r["status"] for r in records]
    assert "cancelled" not in statuses, f"phantom cancelled survived: {records}"
    assert statuses.count("ok") == 1


def test_reconcile_patches_truly_orphaned_started(isolated_runs: Path) -> None:
    """Original behaviour preserved: a started record with NO matching final
    (real orphan from a server crash) still gets marked cancelled."""
    long_ago = time.time() - 6 * 3600
    cron_store.append_run(_started_record("orphan-run-id", long_ago))

    cron_scheduler.reconcile_orphans()

    records = list(cron_store._iter_runs())
    assert len(records) == 1
    assert records[0]["status"] == "cancelled"
    assert records[0]["error"] == "orchestrator restarted"
    assert records[0]["finished_at"] is not None


def test_reconcile_leaves_fresh_started_alone(isolated_runs: Path) -> None:
    """A started record younger than the 5-min cutoff is left untouched —
    the run may still be in flight."""
    just_now = time.time() - 30
    cron_store.append_run(_started_record("in-flight-run", just_now))

    cron_scheduler.reconcile_orphans()

    records = list(cron_store._iter_runs())
    assert len(records) == 1
    assert records[0]["status"] == "running"
    assert records[0]["finished_at"] is None


def test_reconcile_cleans_up_legacy_patched_cancelled(isolated_runs: Path) -> None:
    """Retroactive cleanup for the pre-fix bug: runs.jsonl already contains
    a 'cancelled' record (status=cancelled, error='orchestrator restarted',
    finished_at = stamped hours later) alongside the real ok sibling. The
    next reconcile pass must drop the legacy cancelled so it stops winning
    the dedup tiebreak."""
    long_ago = time.time() - 6 * 3600
    run_id = "legacy-bug-run"
    # Already-patched cancelled record (what's in production runs.jsonl today)
    legacy_cancelled = {
        "run_id": run_id,
        "job_id": "my-project-decide",
        "started_at": long_ago,
        "finished_at": long_ago + 19_000_000 / 1000.0,
        "duration_ms": 19_000_000,
        "status": "cancelled",
        "trigger": "scheduled",
        "session_id": None,
        "exit_code": None,
        "stderr_tail": "",
        "error": "orchestrator restarted",
    }
    cron_store.append_run(legacy_cancelled)
    cron_store.append_run(_final_record(run_id, long_ago))

    cron_scheduler.reconcile_orphans()

    records = list(cron_store._iter_runs())
    statuses = [r["status"] for r in records]
    assert statuses == ["ok"], f"legacy cancelled not cleaned up: {records}"


def test_reconcile_keeps_legacy_cancelled_with_no_sibling(isolated_runs: Path) -> None:
    """If a legacy patched-cancelled has NO real-outcome sibling (a genuine
    orphan from a real crash), keep it — that history is meaningful."""
    long_ago = time.time() - 6 * 3600
    legacy_orphan = {
        "run_id": "real-crash",
        "job_id": "some-other-job",
        "started_at": long_ago,
        "finished_at": long_ago + 1000,
        "duration_ms": 1_000_000,
        "status": "cancelled",
        "trigger": "scheduled",
        "session_id": None,
        "exit_code": None,
        "stderr_tail": "",
        "error": "orchestrator restarted",
    }
    cron_store.append_run(legacy_orphan)

    cron_scheduler.reconcile_orphans()

    records = list(cron_store._iter_runs())
    assert len(records) == 1
    assert records[0]["status"] == "cancelled"


def test_start_registers_periodic_reconcile_job(
    isolated_runs: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Periodic reconcile MUST be registered when the scheduler starts —
    otherwise runs.jsonl accumulates placeholder/final pairs every fire
    until the next service restart (which can be days/weeks).
    """
    import asyncio

    # Point sidecar at an isolated jobs.json so we don't read live jobs.
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text("{}")
    monkeypatch.setattr(cron_store, "JOBS_PATH", jobs_path)

    async def _run() -> None:
        # Make sure no scheduler is left over from a prior test.
        if cron_scheduler._scheduler is not None:
            await cron_scheduler.shutdown()
        try:
            await cron_scheduler.start()
            sched = cron_scheduler._scheduler
            assert sched is not None
            internal = sched.get_job(cron_scheduler._PERIODIC_RECONCILE_JOB_ID)
            assert internal is not None, "periodic reconcile job not registered"
            assert internal.trigger.__class__.__name__ == "IntervalTrigger"
            assert internal.trigger.interval.total_seconds() == 600
        finally:
            await cron_scheduler.shutdown()

    asyncio.run(_run())


def test_dedupe_after_reconcile_yields_only_ok(isolated_runs: Path) -> None:
    """End-to-end: after reconcile cleans up, the dedupe layer returns
    exactly the ok record (which is what the UI displays)."""
    long_ago = time.time() - 6 * 3600
    run_id = "round-trip-run"
    cron_store.append_run(_started_record(run_id, long_ago))
    cron_store.append_run(_final_record(run_id, long_ago, duration_ms=19_900))
    cron_scheduler.reconcile_orphans()
    cron_store._RUNS_CACHE["key"] = None
    cron_store._RUNS_CACHE["deduped"] = None

    deduped = cron_store.list_runs(job_id="my-project-decide", limit=10)
    assert len(deduped) == 1
    assert deduped[0]["status"] == "ok"
    assert deduped[0]["duration_ms"] == 19_900


def test_concurrent_append_survives_prune(isolated_runs: Path) -> None:
    """Regression (code-review HIGH): prune_runs snapshots runs.jsonl then
    os.replaces it; reconcile_orphans does the same from an APScheduler worker
    thread. An append_run landing between a rewriter's snapshot and its replace
    is silently dropped UNLESS all writers share a file lock. Hammer append_run
    against a continuously-running prune and assert nothing is lost.

    With ``_RUNS_FILE_LOCK`` guarding every writer this passes deterministically
    (an append is ordered either before the snapshot or after the replace); the
    pre-fix lock-free code drops records under this load.
    """
    import threading

    old = time.time() - 40 * 86400  # 40 days → past the 30-day retention cutoff
    # Seed trimmable fillers so the first prune does a real (slow) rewrite,
    # widening the snapshot→replace window the appender races against.
    for i in range(1000):
        cron_store.append_run(_final_record(f"seed-{i}", old))
    cron_store._RUNS_CACHE["key"] = None

    tracked = [f"tracked-{i}" for i in range(500)]
    errors: list[Exception] = []

    def appender() -> None:
        try:
            for i, rid in enumerate(tracked):
                # Recent started_at → ALWAYS within the retention window, so a
                # correct prune must keep every one of these.
                cron_store.append_run(_final_record(rid, time.time()))
                # An old filler too, so prune always has something to drop and
                # keeps rewriting throughout the run (max race overlap).
                cron_store.append_run(_final_record(f"old-{i}", old))
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    stop = threading.Event()

    def pruner() -> None:
        try:
            while not stop.is_set():
                cron_store.prune_runs()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    tp = threading.Thread(target=pruner)
    ta = threading.Thread(target=appender)
    tp.start()
    ta.start()
    ta.join()
    stop.set()
    tp.join()

    assert not errors, errors
    surviving = {r.get("run_id") for r in cron_store._iter_runs()}
    missing = [rid for rid in tracked if rid not in surviving]
    assert not missing, (
        f"{len(missing)}/{len(tracked)} concurrently-appended records were "
        f"dropped by a racing prune (e.g. {missing[:3]})"
    )
